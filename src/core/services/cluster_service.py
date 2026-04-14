from datetime import timedelta

from django.utils import timezone

from core.models import GeoCluster, GeoData

from .contracts import OutbreakAnalysis, VisitOutbreakContext
from .spatial import distance_km


ALERT_LEVEL_TO_RISK_LEVEL = {
    "no_alert": 0,
    "low": 1,
    "medium": 2,
    "high": 4,
    "critical": 5,
}


def _find_existing_cluster(
    *,
    context: VisitOutbreakContext,
) -> GeoCluster | None:
    recent_clusters = GeoCluster.objects.filter(
        disease_id=context.disease_id,
        generated_at__gte=timezone.now() - timedelta(days=30),
    ).order_by("-generated_at")

    for cluster in recent_clusters:
        threshold_radius = max(cluster.radius, 3.0)
        if distance_km(
            context.latitude,
            context.longitude,
            cluster.center_lat,
            cluster.center_long,
        ) <= threshold_radius:
            return cluster

    return None


def create_cluster_from_analysis(
    *,
    context: VisitOutbreakContext,
    analysis: OutbreakAnalysis,
) -> int:
    """
    Persist a geographic cluster for a completed outbreak analysis.

    Returns the created cluster id.
    """
    if not analysis.should_create_cluster:
        raise ValueError("This analysis does not require a geographic cluster.")
    existing_cluster = _find_existing_cluster(context=context)

    matched_geodata = list(
        GeoData.objects.filter(visit_id__in=analysis.matched_case_ids).values_list(
            "latitude",
            "longitude",
        )
    )
    coordinates = [(context.latitude, context.longitude), *matched_geodata]

    center_lat = sum(point[0] for point in coordinates) / len(coordinates)
    center_long = sum(point[1] for point in coordinates) / len(coordinates)
    radius = max(
        distance_km(center_lat, center_long, point[0], point[1])
        for point in coordinates
    )

    local_case_count = int(analysis.metadata.get("local_case_count", analysis.nearby_case_count + 1))
    calculated_risk_level = ALERT_LEVEL_TO_RISK_LEVEL.get(analysis.alert_level, 0)

    if existing_cluster is None:
        cluster = GeoCluster.objects.create(
            center_lat=round(center_lat, 6),
            center_long=round(center_long, 6),
            radius=round(max(radius, 0.1), 4),
            disease_id=context.disease_id,
            case_count=local_case_count,
            risk_level=calculated_risk_level,
        )
        return cluster.id

    existing_cluster.center_lat = round(
        (existing_cluster.center_lat + center_lat) / 2,
        6,
    )
    existing_cluster.center_long = round(
        (existing_cluster.center_long + center_long) / 2,
        6,
    )
    existing_cluster.radius = round(max(existing_cluster.radius, radius, 0.1), 4)
    existing_cluster.case_count = max(existing_cluster.case_count, local_case_count)
    existing_cluster.risk_level = max(existing_cluster.risk_level, calculated_risk_level)
    existing_cluster.save()
    return existing_cluster.id
