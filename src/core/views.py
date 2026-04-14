from dataclasses import asdict, replace
from datetime import timedelta

from django.db.models import Count
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_date
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .filters import GeoDataFilter, VisitFilter
from .models import Disease, Doctor, GeoCluster, GeoData, Hospital, LabTest, Patient, Report, Visit
from .permissions import IsAdminOnly, IsDoctorOrAdmin
from .serializers import (
    DiseaseSerializer,
    DoctorSerializer,
    GeoClusterSerializer,
    GeoDataSerializer,
    HospitalSerializer,
    LabTestSerializer,
    PatientSerializer,
    ReportSerializer,
    VisitSerializer,
)
from .services import VisitOutbreakContext
from .services.alert_policies import get_policy_for_disease
from .services.cluster_service import create_cluster_from_analysis
from .services.outbreak_engine import evaluate_visit_outbreak
from .services.report_service import create_report_from_analysis
from .services.trend import build_trend_snapshot


def _report_alert_level(*, risk_score: float, current_case_count: int, surge_ratio: float) -> str:
    if risk_score >= 80 or current_case_count >= 6 or surge_ratio >= 2.5:
        return "critical"
    if risk_score >= 45 or current_case_count >= 3 or surge_ratio >= 1.5:
        return "high"
    if risk_score >= 20 or current_case_count >= 1:
        return "medium"
    return "low"


def _resolve_report_dates(*, visits, date_from: str | None, date_to: str | None):
    first_visit = visits.order_by("diagnosis_date").first()
    last_visit = visits.order_by("-diagnosis_date").first()

    if first_visit is None or last_visit is None:
        return None, None

    analysis_period_start = parse_date(date_from) if date_from else first_visit.diagnosis_date
    analysis_period_end = parse_date(date_to) if date_to else last_visit.diagnosis_date

    if analysis_period_start is None or analysis_period_end is None:
        return None, None

    return analysis_period_start, analysis_period_end


def _build_report_reasons(*, disease, region_type: str | None, current_case_count: int, trend) -> list[str]:
    reasons = [
        f"Report generated for disease {disease.name}.",
        f"Current period captured {current_case_count} case(s).",
    ]

    if region_type:
        reasons.append(f"Region type filter applied: {region_type}.")
    if trend.previous_count == 0 and trend.current_count > 0:
        reasons.append("Current period has no previous baseline cases.")
    elif trend.surge_ratio >= 1.5:
        reasons.append(f"Temporal surge ratio reached {trend.surge_ratio}.")
    elif trend.growth_rate > 0:
        reasons.append(f"Growth rate reached {trend.growth_rate}.")

    return reasons


def _to_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class PatientViewSet(viewsets.ModelViewSet):
    queryset = Patient.objects.all()
    serializer_class = PatientSerializer
    permission_classes = [IsDoctorOrAdmin]


class DoctorViewSet(viewsets.ModelViewSet):
    queryset = Doctor.objects.all()
    serializer_class = DoctorSerializer
    permission_classes = [IsAuthenticated]


class HospitalViewSet(viewsets.ModelViewSet):
    queryset = Hospital.objects.all()
    serializer_class = HospitalSerializer
    permission_classes = [IsAuthenticated]


class DiseaseViewSet(viewsets.ModelViewSet):
    queryset = Disease.objects.all()
    serializer_class = DiseaseSerializer
    permission_classes = [IsAuthenticated]


class VisitViewSet(viewsets.ModelViewSet):
    queryset = Visit.objects.all()
    serializer_class = VisitSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_class = VisitFilter
    permission_classes = [IsDoctorOrAdmin]

    @action(detail=False, methods=["get"])
    def cases_by_disease(self, request):
        visits = VisitFilter(request.GET, queryset=Visit.objects.all()).qs
        qs = (
            visits.values("disease__id", "disease__name")
            .annotate(total_cases=Count("id"))
            .order_by("-total_cases")
        )
        return Response(qs)

    @action(detail=False, methods=["get"])
    def cases_over_time(self, request):
        visits = VisitFilter(request.GET, queryset=Visit.objects.all()).qs
        qs = (
            visits.values("diagnosis_date")
            .annotate(total_cases=Count("id"))
            .order_by("diagnosis_date")
        )
        return Response(qs)

    @action(detail=False, methods=["get"])
    def cases_by_doctor(self, request):
        visits = VisitFilter(request.GET, queryset=Visit.objects.all()).qs
        qs = (
            visits.values("doctor__id", "doctor__user__username")
            .annotate(total_cases=Count("id"))
            .order_by("-total_cases")
        )
        return Response(qs)

    @action(detail=False, methods=["get"])
    def cases_by_region_type(self, request):
        visits = VisitFilter(request.GET, queryset=Visit.objects.all()).qs
        geodata = GeoData.objects.filter(visit__in=visits)
        geodata = GeoDataFilter(request.GET, queryset=geodata).qs
        qs = (
            geodata.values("region_type")
            .annotate(total_cases=Count("visit"))
            .order_by("-total_cases")
        )
        return Response(qs)

    @action(detail=False, methods=["get"])
    def disease_region_matrix(self, request):
        visits = VisitFilter(request.GET, queryset=Visit.objects.all()).qs
        geodata = GeoData.objects.filter(visit__in=visits)
        geodata = GeoDataFilter(request.GET, queryset=geodata).qs
        qs = (
            geodata.values("visit__disease__name", "region_type")
            .annotate(total_cases=Count("visit"))
            .order_by("-total_cases")
        )
        return Response(qs)

    @action(detail=True, methods=["get", "post"])
    def evaluate_outbreak(self, request, pk=None):
        visit = self.get_object()
        payload = request.data if request.method == "POST" else request.query_params
        save_report = _to_bool(payload.get("save_report"))
        save_cluster = _to_bool(payload.get("save_cluster"))

        geodata_queryset = GeoData.objects.filter(visit=visit)
        geodata_id = payload.get("geodata_id")
        region_type_filter = payload.get("region_type")

        if geodata_id:
            geodata_queryset = geodata_queryset.filter(id=geodata_id)
        elif region_type_filter:
            geodata_queryset = geodata_queryset.filter(region_type=region_type_filter)

        geodata = geodata_queryset.order_by("id").first()
        if geodata is None:
            return Response(
                {"error": "No GeoData record was found for this visit."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        policy = get_policy_for_disease(
            disease_code=visit.disease.disease_code,
            risk_level=visit.disease.risk_level,
            infection_score=visit.disease.infection_score,
            high_priority=visit.disease.high_priority,
            rare_disease=visit.disease.rare_disease,
            policy_profile=visit.disease.policy_profile,
        )
        if region_type_filter:
            policy = replace(policy, region_type=region_type_filter)

        context = VisitOutbreakContext(
            visit_id=visit.id,
            patient_id=visit.patient_id,
            doctor_id=visit.doctor_id,
            disease_id=visit.disease_id,
            disease_code=visit.disease.disease_code,
            diagnosis_date=visit.diagnosis_date,
            latitude=geodata.latitude,
            longitude=geodata.longitude,
            region_type=geodata.region_type,
        )
        analysis = evaluate_visit_outbreak(context=context, policy=policy)

        persistence = {
            "report_id": None,
            "cluster_id": None,
            "report_saved": False,
            "cluster_saved": False,
            "messages": [],
        }
        if save_report or save_cluster:
            if request.user.role != "admin":
                return Response(
                    {"error": "Only admin can persist outbreak outputs."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            if save_report:
                if analysis.should_create_report:
                    persistence["report_id"] = create_report_from_analysis(
                        context=context,
                        analysis=analysis,
                    )
                    persistence["report_saved"] = True
                else:
                    persistence["messages"].append("Analysis did not meet report creation conditions.")

            if save_cluster:
                if analysis.should_create_cluster:
                    persistence["cluster_id"] = create_cluster_from_analysis(
                        context=context,
                        analysis=analysis,
                    )
                    persistence["cluster_saved"] = True
                else:
                    persistence["messages"].append("Analysis did not meet cluster creation conditions.")

        return Response(
            {
                "visit_id": visit.id,
                "disease": {
                    "id": visit.disease_id,
                    "code": visit.disease.disease_code,
                    "name": visit.disease.name,
                },
                "geodata": {
                    "id": geodata.id,
                    "latitude": geodata.latitude,
                    "longitude": geodata.longitude,
                    "region_type": geodata.region_type,
                },
                "policy": asdict(policy),
                "analysis": asdict(analysis),
                "persistence": persistence,
            }
        )


class GeoDataViewSet(viewsets.ModelViewSet):
    queryset = GeoData.objects.all()
    serializer_class = GeoDataSerializer
    permission_classes = [IsDoctorOrAdmin]


class GeoClusterViewSet(viewsets.ModelViewSet):
    queryset = GeoCluster.objects.all()
    serializer_class = GeoClusterSerializer
    permission_classes = [IsAdminOnly]

    @action(detail=False, methods=["get"])
    def active_hotspots(self, request):
        clusters = self.get_queryset().filter(risk_level__gte=2)
        disease_id = request.query_params.get("disease")
        min_risk_level = request.query_params.get("min_risk_level")

        if disease_id:
            clusters = clusters.filter(disease_id=disease_id)
        if min_risk_level:
            clusters = clusters.filter(risk_level__gte=min_risk_level)

        clusters = clusters.order_by("-risk_level", "-case_count", "-generated_at")
        serializer = self.get_serializer(clusters, many=True)
        return Response(serializer.data)


class LabTestViewSet(viewsets.ModelViewSet):
    queryset = LabTest.objects.all()
    serializer_class = LabTestSerializer
    permission_classes = [IsDoctorOrAdmin]


class ReportViewSet(viewsets.ModelViewSet):
    queryset = Report.objects.all()
    serializer_class = ReportSerializer
    permission_classes = [IsAdminOnly]

    @action(detail=False, methods=["get"])
    def active_alerts(self, request):
        reports = self.get_queryset().filter(
            status__in=("new", "reviewed"),
            alert_level__in=("medium", "high", "critical"),
        )
        disease_id = request.query_params.get("disease")
        alert_level = request.query_params.get("alert_level")
        status_filter = request.query_params.get("status")

        if disease_id:
            reports = reports.filter(disease_id=disease_id)
        if alert_level:
            reports = reports.filter(alert_level=alert_level)
        if status_filter:
            reports = reports.filter(status=status_filter)

        reports = reports.order_by("-risk_score", "-generated_at")
        serializer = self.get_serializer(reports, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=["post", "get"])
    def generate(self, request):
        payload = request.data if request.method == "POST" else request.query_params

        disease_id = payload.get("disease")
        if not disease_id:
            return Response(
                {"error": "Please provide a disease ID"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        disease = get_object_or_404(Disease, id=disease_id)
        region_type = payload.get("region_type")
        date_from = payload.get("date_from")
        date_to = payload.get("date_to")

        visits = Visit.objects.filter(disease_id=disease_id)
        if date_from:
            visits = visits.filter(diagnosis_date__gte=date_from)
        if date_to:
            visits = visits.filter(diagnosis_date__lte=date_to)
        if region_type:
            visits = visits.filter(geodata__region_type=region_type).distinct()

        if not visits.exists():
            return Response(
                {"error": "no data"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        analysis_period_start, analysis_period_end = _resolve_report_dates(
            visits=visits,
            date_from=date_from,
            date_to=date_to,
        )
        if analysis_period_start is None or analysis_period_end is None:
            return Response(
                {"error": "Invalid report date range."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        window_days = max((analysis_period_end - analysis_period_start).days, 1)
        previous_period_end = analysis_period_start - timedelta(days=1)
        previous_period_start = previous_period_end - timedelta(days=window_days)

        trend = build_trend_snapshot(
            disease_id=disease.id,
            current_start=analysis_period_start,
            current_end=analysis_period_end,
            previous_start=previous_period_start,
            previous_end=previous_period_end,
            region_type=region_type,
        )

        risk_score = trend.current_count * disease.infection_score * disease.risk_level
        alert_level = _report_alert_level(
            risk_score=risk_score,
            current_case_count=trend.current_count,
            surge_ratio=trend.surge_ratio,
        )
        summary = (
            f"Disease {disease.name} recorded {trend.current_count} case(s) "
            f"between {analysis_period_start} and {analysis_period_end} "
            f"with alert level {alert_level}."
        )

        report = Report.objects.create(
            disease=disease,
            trigger_visit=None,
            analysis_period_start=analysis_period_start,
            analysis_period_end=analysis_period_end,
            alert_level=alert_level,
            summary=summary,
            risk_score=risk_score,
            nearby_case_count=0,
            current_case_count=trend.current_count,
            previous_case_count=trend.previous_count,
            growth_rate=trend.growth_rate,
            surge_ratio=trend.surge_ratio,
            reasons=_build_report_reasons(
                disease=disease,
                region_type=region_type,
                current_case_count=trend.current_count,
                trend=trend,
            ),
            status="new",
        )

        serializer = ReportSerializer(report)
        return Response(serializer.data)
