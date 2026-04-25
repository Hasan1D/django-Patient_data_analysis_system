from datetime import timedelta
from math import asin, cos, radians, sin, sqrt

from core.models import GeoData, Visit

from .active_cases import active_visits_queryset
from .contracts import NearbyCase, VisitOutbreakContext


def distance_km(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> float:
    if any(value is None for value in (lat1, lon1, lat2, lon2)):
        raise ValueError("All coordinates must be provided.")

    earth_radius_km = 6371.0

    delta_lat = radians(lat2 - lat1)
    delta_lon = radians(lon2 - lon1)
    lat1_rad = radians(lat1)
    lat2_rad = radians(lat2)

    haversine_value = (
        sin(delta_lat / 2) ** 2
        + cos(lat1_rad) * cos(lat2_rad) * sin(delta_lon / 2) ** 2
    )
    angular_distance = 2 * asin(sqrt(haversine_value))

    return earth_radius_km * angular_distance


def find_nearby_cases(
    *,
    context: VisitOutbreakContext,
    radius_km: float,
    lookback_days: int,
    region_type: str | None = None,
) -> list[NearbyCase]:
    if radius_km < 0:
        raise ValueError("radius_km cannot be negative.")
    if lookback_days < 0:
        raise ValueError("lookback_days cannot be negative.")

    window_start = context.diagnosis_date - timedelta(days=lookback_days)

    visits = Visit.objects.filter(
        disease_id=context.disease_id,
        diagnosis_date__gte=window_start,
        diagnosis_date__lte=context.diagnosis_date,
    )

    active_visit_ids = active_visits_queryset(visits).values("id")
    candidates = GeoData.objects.select_related("visit").filter(visit_id__in=active_visit_ids)
    if context.visit_id:
        candidates = candidates.exclude(visit_id=context.visit_id)
    if region_type:
        candidates = candidates.filter(region_type=region_type)

    nearby_cases_by_visit: dict[int, NearbyCase] = {}

    for candidate in candidates:
        candidate_distance = distance_km(
            context.latitude,
            context.longitude,
            candidate.latitude,
            candidate.longitude,
        )
        if candidate_distance > radius_km:
            continue

        nearby_case = NearbyCase(
            visit_id=candidate.visit_id,
            patient_id=candidate.patient_id,
            diagnosis_date=candidate.visit.diagnosis_date,
            latitude=candidate.latitude,
            longitude=candidate.longitude,
            region_type=candidate.region_type,
            distance_km=round(candidate_distance, 4),
        )
        current_nearest = nearby_cases_by_visit.get(candidate.visit_id)
        if current_nearest is None or nearby_case.distance_km < current_nearest.distance_km:
            nearby_cases_by_visit[candidate.visit_id] = nearby_case

    return sorted(
        nearby_cases_by_visit.values(),
        key=lambda item: (item.distance_km, item.diagnosis_date, item.visit_id),
    )
