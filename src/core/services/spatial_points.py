from dataclasses import dataclass
from datetime import timedelta

from django.db.models import Max

from core.models import GeoData, Visit

from .active_cases import active_geodata_queryset, one_geodata_per_visit_queryset


@dataclass(frozen=True)
class SpatialPoint:
    geodata_id: int
    visit_id: int
    patient_id: int
    disease_id: int
    disease_code: str
    diagnosis_date: object
    latitude: float
    longitude: float
    region_type: str


def resolve_spatial_date_window(*, disease_id: int | None, lookback_days: int | None, date_from, date_to):
    if date_from or date_to or lookback_days is None:
        return date_from, date_to

    queryset = GeoData.objects.all()
    if disease_id is not None:
        queryset = queryset.filter(visit__disease_id=disease_id)

    latest_date = queryset.aggregate(max_date=Max("visit__diagnosis_date"))["max_date"]
    if latest_date is None:
        return None, None

    return latest_date - timedelta(days=lookback_days), latest_date


def collect_active_spatial_points(
    *,
    disease_id: int | None = None,
    lookback_days: int | None = None,
    date_from=None,
    date_to=None,
    region_type: str | None = None,
    one_per_visit: bool = True,
) -> list[SpatialPoint]:
    resolved_date_from, resolved_date_to = resolve_spatial_date_window(
        disease_id=disease_id,
        lookback_days=lookback_days,
        date_from=date_from,
        date_to=date_to,
    )

    visits = Visit.objects.all()
    if disease_id is not None:
        visits = visits.filter(disease_id=disease_id)
    if resolved_date_from is not None:
        visits = visits.filter(diagnosis_date__gte=resolved_date_from)
    if resolved_date_to is not None:
        visits = visits.filter(diagnosis_date__lte=resolved_date_to)

    geodata = active_geodata_queryset(
        GeoData.objects.select_related("visit__disease", "patient").all(),
        visit_queryset=visits,
        constrain_latest_to_queryset=False,
    )
    if region_type:
        geodata = geodata.filter(region_type=region_type)
    if one_per_visit:
        geodata = one_geodata_per_visit_queryset(geodata)

    geodata = geodata.order_by("visit__diagnosis_date", "id")

    return [
        SpatialPoint(
            geodata_id=record.id,
            visit_id=record.visit_id,
            patient_id=record.patient_id,
            disease_id=record.visit.disease_id,
            disease_code=record.visit.disease.disease_code,
            diagnosis_date=record.visit.diagnosis_date,
            latitude=record.latitude,
            longitude=record.longitude,
            region_type=record.region_type,
        )
        for record in geodata
    ]
