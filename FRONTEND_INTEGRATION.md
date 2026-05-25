# React Frontend Integration

## Backend URL

Local development backend:

```text
http://localhost:8000
```

If the React app runs on Vite, CRA, or Next local ports, CORS is already enabled for:

```text
http://localhost:5173
http://127.0.0.1:5173
http://localhost:3000
http://127.0.0.1:3000
http://localhost:4173
http://127.0.0.1:4173
```

For another frontend URL, set:

```powershell
$env:DJANGO_CORS_ALLOWED_ORIGINS="http://localhost:5174,http://127.0.0.1:5174"
```

## Authentication

Login:

```http
POST /api/token/
Content-Type: application/json

{
  "username": "doctor1",
  "password": "secret123"
}
```

Use the returned access token on protected requests:

```http
Authorization: Bearer ACCESS_TOKEN
```

Refresh token:

```http
POST /api/token/refresh/
Content-Type: application/json

{
  "refresh": "REFRESH_TOKEN"
}
```

## Registration Flow

Doctor registration:

```http
POST /api/auth/register/
```

Email verification:

```http
POST /api/auth/verify-email/
```

Resend verification code:

```http
POST /api/auth/resend-verification/
```

Registration hospital dropdown:

```http
GET /api/auth/registration-hospitals/
```

## Main API Groups

```text
/api/patients/
/api/doctors/
/api/doctors/me/
/api/hospitals/
/api/diseases/
/api/visits/
/api/geodata/
/api/geoclusters/
/api/reports/
/api/lab-tests/
/api/maps/cases/
/api/maps/active-cases/
```

Patient visit history:

```http
GET /api/patients/{id}/visits/
```

Each visit includes `doctor_info` with the doctor's name, username,
specialization, hospital id, and hospital name.

Current doctor profile:

```http
GET /api/doctors/me/
PATCH /api/doctors/me/
```

## Useful Analytics Endpoints

```text
GET /api/visits/cases_by_disease/
GET /api/visits/cases_over_time/
GET /api/visits/cases_by_doctor/
GET /api/visits/cases_by_region_type/
GET /api/visits/disease_region_matrix/
GET /api/reports/dashboard_summary/
GET /api/geoclusters/active_hotspots/
GET /api/geoclusters/detect_dbscan/
GET /api/geoclusters/detect_hdbscan/
```

## WebSocket

Active cases map stream:

```text
ws://localhost:8000/ws/maps/active-cases/?token=ACCESS_TOKEN
```

Optional query filters:

```text
disease_code=MEA
disease_type=viral
date_from=2026-04-01
date_to=2026-04-30
```
