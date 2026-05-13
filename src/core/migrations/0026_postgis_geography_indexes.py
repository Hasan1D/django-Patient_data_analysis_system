from django.db import migrations


POSTGIS_SQL = (
    "CREATE EXTENSION IF NOT EXISTS postgis",
    """
    ALTER TABLE core_geodata
        ADD COLUMN IF NOT EXISTS location geography(Point, 4326)
        GENERATED ALWAYS AS (
            ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography
        ) STORED
    """,
    """
    CREATE INDEX IF NOT EXISTS core_geodata_location_gist
        ON core_geodata
        USING GIST (location)
    """,
    """
    ALTER TABLE core_geocluster
        ADD COLUMN IF NOT EXISTS center_location geography(Point, 4326)
        GENERATED ALWAYS AS (
            ST_SetSRID(ST_MakePoint(center_long, center_lat), 4326)::geography
        ) STORED
    """,
    """
    CREATE INDEX IF NOT EXISTS core_geocluster_center_location_gist
        ON core_geocluster
        USING GIST (center_location)
    """,
)

REVERSE_POSTGIS_SQL = (
    "DROP INDEX IF EXISTS core_geocluster_center_location_gist",
    "ALTER TABLE core_geocluster DROP COLUMN IF EXISTS center_location",
    "DROP INDEX IF EXISTS core_geodata_location_gist",
    "ALTER TABLE core_geodata DROP COLUMN IF EXISTS location",
)


def enable_postgis_geography(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        for statement in POSTGIS_SQL:
            cursor.execute(statement)


def disable_postgis_geography(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        for statement in REVERSE_POSTGIS_SQL:
            cursor.execute(statement)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0025_user_email_admin_approval"),
    ]

    operations = [
        migrations.RunPython(enable_postgis_geography, disable_postgis_geography),
    ]
