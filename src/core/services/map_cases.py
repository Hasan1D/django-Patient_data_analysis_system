from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Iterable

from django.db.models import QuerySet

from core.models import GeoData, Visit
from core.services.active_cases import active_geodata_queryset


CASE_EVENT_ADDED = "case.added"
CASE_EVENT_UPDATED = "case.updated"
CASE_EVENT_REMOVED = "case.removed"
CASE_EVENT_TYPES = {CASE_EVENT_ADDED, CASE_EVENT_UPDATED, CASE_EVENT_REMOVED}

MAP_ACTIVE_ALL_GROUP = "map.active.all"
MAP_ACTIVE_DISEASE_GROUP_PREFIX = "map.active.disease."
MAP_ACTIVE_TYPE_GROUP_PREFIX = "map.active.type."


@dataclass(frozen=True)
class MapCaseFilters:
    disease_code: str | None = None
    disease_type: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    region_type: str | None = None
    status: str | None = None
    limit: int = 5000


def normalize_disease_code(value: str | None) -> str | None:
    if value is None:
        return None
    normalized_value = value.strip().upper()
    return normalized_value or None


def normalize_disease_type(value: str | None) -> str | None:
    if value is None:
        return None
    normalized_value = value.strip()
    return normalized_value or None


def map_group_token(value: str | None) -> str:
    normalized_value = (value or "unknown").strip()
    token = re.sub(r"[^a-zA-Z0-9_.-]", "_", normalized_value)
    return token[:80] or "unknown"


def disease_group_name(disease_code: str) -> str:
    return f"{MAP_ACTIVE_DISEASE_GROUP_PREFIX}{map_group_token(disease_code.upper())}"


def disease_type_group_name(disease_type: str) -> str:
    return f"{MAP_ACTIVE_TYPE_GROUP_PREFIX}{map_group_token(disease_type.lower())}"


def active_case_groups_for_case(case: dict) -> set[str]:
    groups = {MAP_ACTIVE_ALL_GROUP}
    disease_code = case.get("disease_code")
    disease_type = case.get("disease_type")
    if disease_code:
        groups.add(disease_group_name(str(disease_code)))
    if disease_type:
        groups.add(disease_type_group_name(str(disease_type)))
    return groups


def _apply_common_filters(queryset: QuerySet, filters: MapCaseFilters) -> QuerySet:
    if filters.disease_code:
        queryset = queryset.filter(visit__disease__disease_code__iexact=filters.disease_code)
    if filters.disease_type:
        queryset = queryset.filter(visit__disease__type__iexact=filters.disease_type)
    if filters.start_date:
        queryset = queryset.filter(visit__diagnosis_date__gte=filters.start_date)
    if filters.end_date:
        queryset = queryset.filter(visit__diagnosis_date__lte=filters.end_date)
    if filters.region_type:
        queryset = queryset.filter(region_type=filters.region_type)
    if filters.status:
        queryset = queryset.filter(visit__status=filters.status)
    return queryset


def historical_map_cases_queryset(filters: MapCaseFilters) -> QuerySet:
    queryset = GeoData.objects.select_related("visit__disease").all()
    queryset = _apply_common_filters(queryset, filters)
    return queryset.order_by("visit__diagnosis_date", "id")[: filters.limit]


def active_map_cases_queryset(filters: MapCaseFilters) -> QuerySet:
    visit_queryset = Visit.objects.all()
    geodata_queryset = GeoData.objects.select_related("visit__disease").all()
    active_geodata = active_geodata_queryset(
        geodata_queryset,
        visit_queryset=visit_queryset,
        constrain_latest_to_queryset=False,
    )
    active_geodata = _apply_common_filters(active_geodata, filters)
    return active_geodata.order_by("visit__diagnosis_date", "id")[: filters.limit]


def is_live_active_geodata(geodata: GeoData) -> bool:
    return active_geodata_queryset(
        GeoData.objects.filter(id=geodata.id),
        visit_queryset=Visit.objects.filter(id=geodata.visit_id),
        constrain_latest_to_queryset=False,
    ).exists()


def is_latest_visit_for_patient_disease(visit: Visit) -> bool:
    latest_visit = (
        Visit.objects.filter(
            patient_id=visit.patient_id,
            disease_id=visit.disease_id,
        )
        .order_by("-diagnosis_date", "-id")
        .values_list("id", flat=True)
        .first()
    )
    return latest_visit == visit.id


def geodata_for_visit_family(visit: Visit) -> QuerySet:
    return GeoData.objects.select_related("visit__disease").filter(
        visit__patient_id=visit.patient_id,
        visit__disease_id=visit.disease_id,
    )


def serialize_map_case(geodata: GeoData) -> dict:
    visit = geodata.visit
    disease = visit.disease
    return {
        "id": geodata.id,
        "geodata_id": geodata.id,
        "visit_id": visit.id,
        "patient_id": geodata.patient_id,
        "disease_id": disease.id,
        "disease_code": disease.disease_code,
        "disease_name": disease.name,
        "disease_type": disease.type,
        "diagnosis_date": visit.diagnosis_date.isoformat(),
        "status": visit.status,
        "region_type": geodata.region_type,
        "lat": geodata.latitude,
        "lng": geodata.longitude,
        "latitude": geodata.latitude,
        "longitude": geodata.longitude,
    }


def map_case_to_feature(case: dict) -> dict:
    properties = dict(case)
    lng = properties.pop("lng")
    lat = properties.pop("lat")
    properties.pop("longitude", None)
    properties.pop("latitude", None)
    return {
        "type": "Feature",
        "id": case["id"],
        "geometry": {
            "type": "Point",
            "coordinates": [lng, lat],
        },
        "properties": properties,
    }


def map_cases_to_feature_collection(geodata_records: Iterable[GeoData]) -> dict:
    features = [
        map_case_to_feature(serialize_map_case(geodata))
        for geodata in geodata_records
    ]
    return {
        "type": "FeatureCollection",
        "features": features,
        "count": len(features),
    }
