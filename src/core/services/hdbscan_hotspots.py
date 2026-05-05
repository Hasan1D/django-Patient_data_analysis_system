from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Iterable

from django.utils import timezone

from core.models import Disease, GeoCluster

from .spatial import distance_km
from .spatial_points import SpatialPoint, collect_active_spatial_points


@dataclass(frozen=True)
class HDBSCANClusterCandidate:
    disease_id: int
    disease_code: str
    point_count: int
    unique_visit_count: int
    unique_patient_count: int
    member_geodata_ids: list[int]
    member_visit_ids: list[int]
    center_lat: float
    center_long: float
    radius_km: float
    risk_level: int
    diagnosis_start: str
    diagnosis_end: str
    region_types: list[str]
    probability: float
    confidence: float


@dataclass(frozen=True)
class HDBSCANDetectionResult:
    clusters: list[HDBSCANClusterCandidate]
    noise_count: int


def _load_hdbscan_dependencies():
    import hdbscan
    import numpy as np

    return np, hdbscan


def _cluster_risk_level(*, disease: Disease, point_count: int) -> int:
    if disease.rare_disease:
        return 5
    if disease.high_priority and point_count >= 2:
        return max(4, min(5, disease.risk_level))
    if point_count >= 6:
        return 5
    if point_count >= 4:
        return max(4, min(5, disease.risk_level))
    return max(2, min(5, disease.risk_level))


def _build_cluster_candidate(
    *,
    disease: Disease,
    points: list[SpatialPoint],
    probabilities: list[float],
) -> HDBSCANClusterCandidate:
    center_lat = sum(point.latitude for point in points) / len(points)
    center_long = sum(point.longitude for point in points) / len(points)
    radius_km = max(
        distance_km(center_lat, center_long, point.latitude, point.longitude)
        for point in points
    )
    diagnosis_dates = sorted(point.diagnosis_date for point in points)
    region_types = sorted({point.region_type for point in points})
    probability = round(float(sum(probabilities) / len(probabilities)), 4) if probabilities else 0.0

    return HDBSCANClusterCandidate(
        disease_id=disease.id,
        disease_code=disease.disease_code,
        point_count=len(points),
        unique_visit_count=len({point.visit_id for point in points}),
        unique_patient_count=len({point.patient_id for point in points}),
        member_geodata_ids=sorted({point.geodata_id for point in points}),
        member_visit_ids=sorted({point.visit_id for point in points}),
        center_lat=round(center_lat, 6),
        center_long=round(center_long, 6),
        radius_km=round(max(radius_km, 0.1), 4),
        risk_level=_cluster_risk_level(disease=disease, point_count=len(points)),
        diagnosis_start=diagnosis_dates[0].isoformat(),
        diagnosis_end=diagnosis_dates[-1].isoformat(),
        region_types=region_types,
        probability=probability,
        confidence=probability,
    )


def detect_hdbscan_clusters(
    *,
    disease_id: int | None = None,
    lookback_days: int | None = 14,
    date_from=None,
    date_to=None,
    region_type: str | None = None,
    min_cluster_size: int = 3,
    min_samples: int | None = None,
    cluster_selection_method: str = "eom",
    allow_single_cluster: bool = False,
    point_mode: str = "exposure",
) -> HDBSCANDetectionResult:
    if min_cluster_size < 2:
        raise ValueError("min_cluster_size must be at least 2.")
    if min_samples is not None and min_samples < 1:
        raise ValueError("min_samples must be at least 1 when provided.")
    if cluster_selection_method not in {"eom", "leaf"}:
        raise ValueError("cluster_selection_method must be 'eom' or 'leaf'.")

    points = collect_active_spatial_points(
        disease_id=disease_id,
        lookback_days=lookback_days,
        date_from=date_from,
        date_to=date_to,
        region_type=region_type,
        point_mode=point_mode,
    )
    if len(points) < min_cluster_size:
        return HDBSCANDetectionResult(clusters=[], noise_count=len(points))

    np, hdbscan = _load_hdbscan_dependencies()

    points_by_disease: dict[int, list[SpatialPoint]] = {}
    for point in points:
        points_by_disease.setdefault(point.disease_id, []).append(point)

    clusters: list[HDBSCANClusterCandidate] = []
    noise_count = 0
    for current_disease_id, disease_points in points_by_disease.items():
        if len(disease_points) < min_cluster_size:
            noise_count += len(disease_points)
            continue

        coordinates = np.radians([[point.latitude, point.longitude] for point in disease_points])
        clusterer = hdbscan.HDBSCAN(
            min_cluster_size=min_cluster_size,
            min_samples=min_samples,
            metric="haversine",
            cluster_selection_method=cluster_selection_method,
            allow_single_cluster=allow_single_cluster,
        )
        labels = list(clusterer.fit_predict(coordinates))
        probabilities = list(getattr(clusterer, "probabilities_", [0.0] * len(labels)))

        disease = Disease.objects.get(id=current_disease_id)
        grouped_indices: dict[int, list[int]] = {}
        for point_index, label in enumerate(labels):
            if label == -1:
                noise_count += 1
                continue
            grouped_indices.setdefault(label, []).append(point_index)

        for cluster_indices in grouped_indices.values():
            member_points = [disease_points[index] for index in cluster_indices]
            member_probabilities = [float(probabilities[index]) for index in cluster_indices]
            clusters.append(
                _build_cluster_candidate(
                    disease=disease,
                    points=member_points,
                    probabilities=member_probabilities,
                )
            )

    clusters.sort(key=lambda cluster: (-cluster.risk_level, -cluster.point_count, cluster.disease_code))
    return HDBSCANDetectionResult(clusters=clusters, noise_count=noise_count)


def _find_existing_hdbscan_cluster(*, candidate: HDBSCANClusterCandidate) -> GeoCluster | None:
    recent_clusters = GeoCluster.objects.filter(
        disease_id=candidate.disease_id,
        generated_at__gte=timezone.now() - timedelta(days=30),
    ).order_by("-generated_at")

    for cluster in recent_clusters:
        threshold_radius = max(cluster.radius, candidate.radius_km, 3.0)
        if distance_km(
            candidate.center_lat,
            candidate.center_long,
            cluster.center_lat,
            cluster.center_long,
        ) <= threshold_radius:
            return cluster

    return None


def persist_hdbscan_clusters(*, clusters: Iterable[HDBSCANClusterCandidate]) -> list[int]:
    persisted_ids: list[int] = []

    for candidate in clusters:
        existing_cluster = _find_existing_hdbscan_cluster(candidate=candidate)
        if existing_cluster is None:
            cluster = GeoCluster.objects.create(
                center_lat=candidate.center_lat,
                center_long=candidate.center_long,
                radius=candidate.radius_km,
                disease_id=candidate.disease_id,
                case_count=candidate.unique_visit_count,
                risk_level=candidate.risk_level,
            )
            persisted_ids.append(cluster.id)
            continue

        existing_cluster.center_lat = round((existing_cluster.center_lat + candidate.center_lat) / 2, 6)
        existing_cluster.center_long = round((existing_cluster.center_long + candidate.center_long) / 2, 6)
        existing_cluster.radius = round(max(existing_cluster.radius, candidate.radius_km, 0.1), 4)
        existing_cluster.case_count = max(existing_cluster.case_count, candidate.unique_visit_count)
        existing_cluster.risk_level = max(existing_cluster.risk_level, candidate.risk_level)
        existing_cluster.save()
        persisted_ids.append(existing_cluster.id)

    return persisted_ids


def serialize_hdbscan_clusters(clusters: Iterable[HDBSCANClusterCandidate]) -> list[dict[str, object]]:
    return [asdict(cluster) for cluster in clusters]
