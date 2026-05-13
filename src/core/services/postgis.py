from django.db import connection
from django.db.models import FloatField
from django.db.models.expressions import RawSQL


def postgis_enabled() -> bool:
    return connection.vendor == "postgresql"


def _point_geography_sql() -> str:
    return "ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography"


def filter_geodata_within_radius(queryset, *, latitude: float, longitude: float, radius_km: float):
    if not postgis_enabled():
        return queryset

    return queryset.extra(
        where=[
            f"location IS NOT NULL AND ST_DWithin(location, {_point_geography_sql()}, %s)"
        ],
        params=[longitude, latitude, radius_km * 1000],
    )


def annotate_geodata_distance(queryset, *, latitude: float, longitude: float):
    if not postgis_enabled():
        return queryset

    return queryset.annotate(
        postgis_distance_m=RawSQL(
            f"ST_Distance(location, {_point_geography_sql()})",
            [longitude, latitude],
            output_field=FloatField(),
        )
    )


def filter_geoclusters_within_radius(queryset, *, latitude: float, longitude: float, radius_km: float):
    if not postgis_enabled():
        return queryset

    return queryset.extra(
        where=[
            f"center_location IS NOT NULL AND ST_DWithin(center_location, {_point_geography_sql()}, %s)"
        ],
        params=[longitude, latitude, radius_km * 1000],
    )
