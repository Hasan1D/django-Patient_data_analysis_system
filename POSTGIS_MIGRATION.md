# PostgreSQL and PostGIS Migration

This project keeps `latitude` and `longitude` in the public API so the React
frontend does not need to change immediately. PostGIS is enabled with generated
`geography(Point, 4326)` columns and GiST indexes:

```text
core_geodata.location
core_geocluster.center_location
```

## 1. Install Database Requirements

```powershell
cd "D:\project python\dis_sys"
Scripts\pip.exe install -r requirements.txt
```

## 2. Create PostgreSQL Database

Run in PostgreSQL:

```sql
CREATE DATABASE dis_sys;
```

The migration will run:

```sql
CREATE EXTENSION IF NOT EXISTS postgis;
```

If your database user is not allowed to create extensions, ask the database
admin to run this once before `manage.py migrate`.

## 3. Export SQLite Data

```powershell
cd "D:\project python\dis_sys\src"
..\Scripts\python.exe manage.py dumpdata --natural-foreign --natural-primary --exclude contenttypes --exclude auth.permission -o backup_data.json
```

## 4. Point Django at PostgreSQL

PowerShell example:

```powershell
$env:DJANGO_DATABASE_ENGINE="postgres"
$env:POSTGRES_DB="dis_sys"
$env:POSTGRES_USER="postgres"
$env:POSTGRES_PASSWORD="your-password"
$env:POSTGRES_HOST="localhost"
$env:POSTGRES_PORT="5432"
```

Production should also set:

```powershell
$env:DJANGO_DEBUG="False"
$env:DJANGO_SECRET_KEY="replace-with-a-real-secret"
$env:DJANGO_ALLOWED_HOSTS="api.example.com"
$env:DJANGO_CORS_ALLOWED_ORIGINS="https://frontend.example.com"
```

## 5. Migrate and Import Data

```powershell
..\Scripts\python.exe manage.py migrate
..\Scripts\python.exe manage.py loaddata backup_data.json
```

## 6. Verify

```powershell
..\Scripts\python.exe manage.py check
..\Scripts\python.exe manage.py test core.tests.test_auth core.tests.test_analytics core.tests.test_maps core.tests.test_monitoring core.tests.test_outbreak core.tests.test_services core.tests.test_workflow --failfast
```

## Notes

The code still supports SQLite for local development when
`DJANGO_DATABASE_ENGINE` is not set to `postgres`.

The current PostGIS integration avoids GeoDjango `PointField` because GeoDjango
requires GDAL on Windows. The database still uses real PostGIS geography columns,
and services use PostGIS radius filters when running on PostgreSQL.
