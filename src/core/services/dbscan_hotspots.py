from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Iterable

from django.db.models import Max
from django.utils import timezone

from core.models import Disease, GeoCluster, GeoData

from .spatial import distance_km


@dataclass(frozen=True)
class DBSCANPoint:
    geodata_id: int
    visit_id: int
    patient_id: int
    disease_id: int
    disease_code: str
    diagnosis_date: object
    latitude: float
    longitude: float
    region_type: str


@dataclass(frozen=True)
class DBSCANClusterCandidate:
    disease_id: int
    disease_code: str
    point_count: int
    member_geodata_ids: list[int]
    member_visit_ids: list[int]
    center_lat: float
    center_long: float
    radius_km: float
    risk_level: int
    diagnosis_start: str
    diagnosis_end: str
    region_types: list[str]


def _resolve_date_window(*, disease_id: int | None, lookback_days: int | None, date_from, date_to):
    if date_from or date_to or lookback_days is None:
        return date_from, date_to

    queryset = GeoData.objects.all()
    if disease_id is not None:
        queryset = queryset.filter(visit__disease_id=disease_id)

    latest_date = queryset.aggregate(max_date=Max("visit__diagnosis_date"))["max_date"]
    if latest_date is None:
        return None, None

    return latest_date - timedelta(days=lookback_days), latest_date


def _collect_points(
    *,
    disease_id: int | None = None,
    lookback_days: int | None = None,
    date_from=None,
    date_to=None,
    region_type: str | None = None,
) -> list[DBSCANPoint]:
    resolved_date_from, resolved_date_to = _resolve_date_window(
        disease_id=disease_id,
        lookback_days=lookback_days,
        date_from=date_from,
        date_to=date_to,
    )

    queryset = GeoData.objects.select_related("visit__disease", "patient").all()
    if disease_id is not None:
        queryset = queryset.filter(visit__disease_id=disease_id)
    if resolved_date_from is not None:
        queryset = queryset.filter(visit__diagnosis_date__gte=resolved_date_from)
    if resolved_date_to is not None:
        queryset = queryset.filter(visit__diagnosis_date__lte=resolved_date_to)
    if region_type:
        queryset = queryset.filter(region_type=region_type)

    queryset = queryset.order_by("visit__diagnosis_date", "id")

    return [
        DBSCANPoint(
            geodata_id=geodata.id,
            visit_id=geodata.visit_id,
            patient_id=geodata.patient_id,
            disease_id=geodata.visit.disease_id,
            disease_code=geodata.visit.disease.disease_code,
            diagnosis_date=geodata.visit.diagnosis_date,
            latitude=geodata.latitude,
            longitude=geodata.longitude,
            region_type=geodata.region_type,
        )
        for geodata in queryset
    ]


def _region_query(points: list[DBSCANPoint], point_index: int, eps_km: float, cache: dict[int, list[int]]) -> list[int]:
    if point_index in cache:
        return cache[point_index]

    base_point = points[point_index]
    neighbors = [
        candidate_index
        for candidate_index, candidate in enumerate(points)
        if distance_km(
            base_point.latitude,
            base_point.longitude,
            candidate.latitude,
            candidate.longitude,
        ) <= eps_km
    ]
    cache[point_index] = neighbors
    return neighbors


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


def _build_cluster_candidate(*, disease: Disease, points: list[DBSCANPoint]) -> DBSCANClusterCandidate:
    center_lat = sum(point.latitude for point in points) / len(points)
    center_long = sum(point.longitude for point in points) / len(points)
    radius_km = max(
        distance_km(center_lat, center_long, point.latitude, point.longitude)
        for point in points
    )
    diagnosis_dates = sorted(point.diagnosis_date for point in points)
    region_types = sorted({point.region_type for point in points})

    return DBSCANClusterCandidate(
        disease_id=disease.id,
        disease_code=disease.disease_code,
        point_count=len(points),
        member_geodata_ids=sorted({point.geodata_id for point in points}),
        member_visit_ids=sorted({point.visit_id for point in points}),
        center_lat=round(center_lat, 6),
        center_long=round(center_long, 6),
        radius_km=round(max(radius_km, 0.1), 4),
        risk_level=_cluster_risk_level(disease=disease, point_count=len(points)),
        diagnosis_start=diagnosis_dates[0].isoformat(),
        diagnosis_end=diagnosis_dates[-1].isoformat(),
        region_types=region_types,
    )


def detect_dbscan_clusters(
    *,
    disease_id: int | None = None,
    lookback_days: int | None = 14,
    date_from=None,
    date_to=None,
    region_type: str | None = None,
    eps_km: float = 3.0,
    min_samples: int = 2,
) -> list[DBSCANClusterCandidate]:
    if eps_km <= 0:
        raise ValueError("eps_km must be greater than zero.")
    if min_samples < 2:
        raise ValueError("min_samples must be at least 2.")

    points = _collect_points(
        disease_id=disease_id,
        lookback_days=lookback_days,
        date_from=date_from,
        date_to=date_to,
        region_type=region_type,
    )
    if not points:
        return []

    points_by_disease: dict[int, list[DBSCANPoint]] = {}
    for point in points:
        points_by_disease.setdefault(point.disease_id, []).append(point)

    clusters: list[DBSCANClusterCandidate] = []
    for current_disease_id, disease_points in points_by_disease.items():
        disease = Disease.objects.get(id=current_disease_id)
        labels: list[int | None] = [None] * len(disease_points)
        visited: set[int] = set()
        cluster_id = 0
        neighbor_cache: dict[int, list[int]] = {}

        for point_index in range(len(disease_points)):
            if point_index in visited:
                continue

            visited.add(point_index)
            neighbors = _region_query(disease_points, point_index, eps_km, neighbor_cache)
            if len(neighbors) < min_samples:
                labels[point_index] = -1
                continue

            cluster_id += 1
            labels[point_index] = cluster_id
            seeds = set(neighbors)
            seeds.discard(point_index)

            while seeds:
                seed_index = seeds.pop()

                if seed_index not in visited:
                    visited.add(seed_index)
                    seed_neighbors = _region_query(disease_points, seed_index, eps_km, neighbor_cache)
                    if len(seed_neighbors) >= min_samples:
                        seeds.update(seed_neighbors)

                if labels[seed_index] in (None, -1):
                    labels[seed_index] = cluster_id

        cluster_members: dict[int, list[DBSCANPoint]] = {}
        for member_index, label in enumerate(labels):
            if label is None or label == -1:
                continue
            cluster_members.setdefault(label, []).append(disease_points[member_index])

        for member_points in cluster_members.values():
            clusters.append(_build_cluster_candidate(disease=disease, points=member_points))

    clusters.sort(key=lambda cluster: (-cluster.risk_level, -cluster.point_count, cluster.disease_code))
    return clusters


def _find_existing_dbscan_cluster(*, candidate: DBSCANClusterCandidate) -> GeoCluster | None:
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


def persist_dbscan_clusters(*, clusters: Iterable[DBSCANClusterCandidate]) -> list[int]:
    persisted_ids: list[int] = []

    for candidate in clusters:
        existing_cluster = _find_existing_dbscan_cluster(candidate=candidate)
        if existing_cluster is None:
            cluster = GeoCluster.objects.create(
                center_lat=candidate.center_lat,
                center_long=candidate.center_long,
                radius=candidate.radius_km,
                disease_id=candidate.disease_id,
                case_count=candidate.point_count,
                risk_level=candidate.risk_level,
            )
            persisted_ids.append(cluster.id)
            continue

        existing_cluster.center_lat = round((existing_cluster.center_lat + candidate.center_lat) / 2, 6)
        existing_cluster.center_long = round((existing_cluster.center_long + candidate.center_long) / 2, 6)
        existing_cluster.radius = round(max(existing_cluster.radius, candidate.radius_km, 0.1), 4)
        existing_cluster.case_count = max(existing_cluster.case_count, candidate.point_count)
        existing_cluster.risk_level = max(existing_cluster.risk_level, candidate.risk_level)
        existing_cluster.save()
        persisted_ids.append(existing_cluster.id)

    return persisted_ids


def serialize_dbscan_clusters(clusters: Iterable[DBSCANClusterCandidate]) -> list[dict[str, object]]:
    return [asdict(cluster) for cluster in clusters]
