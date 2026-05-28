# Patient Data Analysis System

Django REST backend for managing patient records, visits, diseases, geospatial case data,
outbreak alerts, realtime active-case maps, and ML-assisted risk analysis.

## Requirements

- Python 3.11+
- SQLite for local development
- PostgreSQL + PostGIS for production geospatial workloads
- Redis for production WebSocket channel layers

## Setup

```powershell
cd C:\Users\MCC\Desktop\project5\django-Patient_data_analysis_system
.\venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example src\.env
.\venv\Scripts\python.exe .\src\manage.py migrate
.\venv\Scripts\python.exe .\src\manage.py createsuperuser
```

## Run

```powershell
.\venv\Scripts\python.exe .\src\manage.py runserver
```

Local API base URL:

```text
http://localhost:8000/api/
```

## Important Environment Variables

- `DJANGO_DEBUG=False` for production.
- `DJANGO_SECRET_KEY` must be set when debug is disabled.
- `DJANGO_ALLOWED_HOSTS` should contain the backend host names.
- `DJANGO_CORS_ALLOWED_ORIGINS` should contain frontend origins.
- `REDIS_URL` is required when debug is disabled.
- `DJANGO_DATABASE_ENGINE=postgres` enables PostgreSQL settings.
- `DJANGO_AUDIT_LOG_ENABLED=True` records API access in `AuditLog`.

## Security Notes

- JWT authentication is enabled for API access.
- Doctors can read data scoped to their hospital; admins can manage global records.
- Doctor, hospital, and disease writes are restricted to admins.
- Email verification, registration, and login endpoints have scoped throttling.
- Audit logs record API method, path, status, user, IP address, and user agent.

## Pagination

List endpoints keep their existing unpaginated shape by default. Clients can opt in:

```http
GET /api/patients/?page=1&page_size=100
```

## API Documentation

Install dependencies from `requirements.txt`. If `drf-spectacular` is available,
the schema and Swagger UI are exposed at:

```text
/api/schema/
/api/docs/
```

## Tests

```powershell
.\venv\Scripts\python.exe .\src\manage.py check
.\venv\Scripts\python.exe .\src\manage.py test
```

## Dummy Data Import

```powershell
.\venv\Scripts\python.exe .\src\manage.py import_dummy_data --data-dir ..\DATA
```

Use `--dry-run` first when checking CSV compatibility:

```powershell
.\venv\Scripts\python.exe .\src\manage.py import_dummy_data --data-dir ..\DATA --dry-run
```
