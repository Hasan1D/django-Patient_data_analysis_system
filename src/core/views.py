from dataclasses import asdict, replace
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, Max, Q
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_date
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .filters import GeoDataFilter, VisitFilter
from .models import Disease, Doctor, GeoCluster, GeoData, Hospital, LabTest, Patient, Report, User, Visit
from .permissions import IsAdminOnly, IsDoctorOrAdmin
from .serializers import (
    AllergySerializer,
    ChronicDiseaseSerializer,
    DiseaseSerializer,
    DoctorSerializer,
    GeoClusterSerializer,
    GeoDataSerializer,
    HospitalSerializer,
    LabTestSerializer,
    PatientAllergyCreateSerializer,
    PatientChronicDiseaseCreateSerializer,
    PatientSurgicalHistoryCreateSerializer,
    PatientSerializer,
    PatientVaccineCreateSerializer,
    PatientVisitCreateSerializer,
    ReportSerializer,
    ResendEmailVerificationSerializer,
    SurgicalHistorySerializer,
    UserRegistrationSerializer,
    UserSerializer,
    VaccineSerializer,
    VisitSerializer,
    VisitLabTestCreateSerializer,
    EmailVerificationSerializer,
)
from .services import VisitOutbreakContext
from .services.active_cases import (
    active_geodata_queryset,
    active_visits_queryset,
    one_geodata_per_visit_queryset,
)
from .services.alert_policies import get_policy_for_disease
from .services.anomaly_detection import (
    detect_temporal_anomalies,
    serialize_anomaly_candidates,
)
from .services.cluster_service import create_cluster_from_analysis
from .services.dbscan_hotspots import (
    detect_dbscan_clusters,
    persist_dbscan_clusters,
    serialize_dbscan_clusters,
)
from .services.hdbscan_hotspots import (
    detect_hdbscan_clusters,
    persist_hdbscan_clusters,
    serialize_hdbscan_clusters,
)
from .services.outbreak_engine import evaluate_visit_outbreak
from .services.report_service import create_report_from_analysis
from .services.risk_prediction import predict_visit_risk, serialize_risk_prediction
from .services.trend import build_trend_snapshot
from .services.workflow_service import (
    create_allergy_for_patient,
    create_chronic_disease_for_patient,
    create_lab_test_for_visit,
    create_surgical_history_for_patient,
    create_vaccine_for_patient,
    create_visit_for_patient,
)
from .services.email_verification import send_email_verification_code, verify_email_code


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


def _build_generated_report_kwargs(*, disease, analysis_period_start, analysis_period_end, region_type: str | None, trend):
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

    return {
        "disease": disease,
        "trigger_visit": None,
        "analysis_period_start": analysis_period_start,
        "analysis_period_end": analysis_period_end,
        "alert_level": alert_level,
        "summary": summary,
        "risk_score": risk_score,
        "nearby_case_count": 0,
        "current_case_count": trend.current_count,
        "previous_case_count": trend.previous_count,
        "growth_rate": trend.growth_rate,
        "surge_ratio": trend.surge_ratio,
        "reasons": _build_report_reasons(
            disease=disease,
            region_type=region_type,
            current_case_count=trend.current_count,
            trend=trend,
        ),
        "status": "new",
    }


def _to_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _to_int(value, default: int | None = None) -> int | None:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _active_alerts_queryset(queryset):
    active_trigger_visit_ids = active_visits_queryset(Visit.objects.all()).values("id")
    return queryset.filter(
        status__in=("new", "reviewed"),
        alert_level__in=("medium", "high", "critical"),
    ).filter(Q(trigger_visit__isnull=True) | Q(trigger_visit_id__in=active_trigger_visit_ids))


def _active_hotspots_queryset(queryset):
    return queryset.filter(risk_level__gte=2)


class RegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = UserRegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            user = serializer.save()
            send_email_verification_code(user)

        return Response(
            {
                "message": "Registration successful. Please check your email for the verification code.",
                "user": UserSerializer(user).data,
            },
            status=status.HTTP_201_CREATED,
        )


class VerifyEmailView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = EmailVerificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            verify_email_code(serializer.user, serializer.validated_data["code"])
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"message": "Account verified successfully."})


class ResendEmailVerificationView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ResendEmailVerificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        send_email_verification_code(serializer.user)

        return Response({"message": "Verification code sent."})


class PatientViewSet(viewsets.ModelViewSet):
    queryset = Patient.objects.all()
    serializer_class = PatientSerializer
    permission_classes = [IsDoctorOrAdmin]

    @action(detail=True, methods=["post"], url_path="visits")
    def visits(self, request, pk=None):
        patient = self.get_object()
        serializer = PatientVisitCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        visit = create_visit_for_patient(
            patient=patient,
            visit_data=serializer.validated_data,
        )
        return Response(VisitSerializer(visit).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="allergies")
    def allergies(self, request, pk=None):
        patient = self.get_object()
        serializer = PatientAllergyCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        allergy = create_allergy_for_patient(
            patient=patient,
            allergy_data=serializer.validated_data,
        )
        return Response(AllergySerializer(allergy).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="chronic-diseases")
    def chronic_diseases(self, request, pk=None):
        patient = self.get_object()
        serializer = PatientChronicDiseaseCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        chronic_disease = create_chronic_disease_for_patient(
            patient=patient,
            chronic_disease_data=serializer.validated_data,
        )
        return Response(
            ChronicDiseaseSerializer(chronic_disease).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="vaccines")
    def vaccines(self, request, pk=None):
        patient = self.get_object()
        serializer = PatientVaccineCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        vaccine = create_vaccine_for_patient(
            patient=patient,
            vaccine_data=serializer.validated_data,
        )
        return Response(VaccineSerializer(vaccine).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="surgeries")
    def surgeries(self, request, pk=None):
        patient = self.get_object()
        serializer = PatientSurgicalHistoryCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        surgical_history = create_surgical_history_for_patient(
            patient=patient,
            surgical_history_data=serializer.validated_data,
        )
        return Response(
            SurgicalHistorySerializer(surgical_history).data,
            status=status.HTTP_201_CREATED,
        )


class UserViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = User.objects.all().order_by("id")
    serializer_class = UserSerializer
    permission_classes = [IsAdminOnly]


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

    @action(detail=True, methods=["post"], url_path="lab-tests")
    def lab_tests(self, request, pk=None):
        visit = self.get_object()
        serializer = VisitLabTestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        lab_test = create_lab_test_for_visit(
            visit=visit,
            lab_test_data=serializer.validated_data,
        )
        return Response(LabTestSerializer(lab_test).data, status=status.HTTP_201_CREATED)

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
            if request.user.role != User.ROLE_ADMIN:
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

    @action(detail=True, methods=["get"])
    def predict_risk(self, request, pk=None):
        visit = self.get_object()
        model_path = request.query_params.get("model_path")

        try:
            prediction = predict_visit_risk(visit=visit, model_path=model_path)
        except FileNotFoundError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(serialize_risk_prediction(prediction))


class GeoDataViewSet(viewsets.ModelViewSet):
    queryset = GeoData.objects.all()
    serializer_class = GeoDataSerializer
    permission_classes = [IsDoctorOrAdmin]

    @action(detail=False, methods=["get"])
    def active(self, request):
        visits = VisitFilter(request.GET, queryset=Visit.objects.all()).qs
        geodata = active_geodata_queryset(
            GeoData.objects.all(),
            visit_queryset=visits,
            constrain_latest_to_queryset=False,
        )
        geodata = GeoDataFilter(request.GET, queryset=geodata).qs
        geodata = one_geodata_per_visit_queryset(geodata).order_by("visit__diagnosis_date", "id")
        serializer = self.get_serializer(geodata, many=True)
        return Response(serializer.data)


class GeoClusterViewSet(viewsets.ModelViewSet):
    queryset = GeoCluster.objects.all()
    serializer_class = GeoClusterSerializer
    permission_classes = [IsAdminOnly]

    @action(detail=False, methods=["get"])
    def active_hotspots(self, request):
        clusters = _active_hotspots_queryset(self.get_queryset())
        disease_id = request.query_params.get("disease")
        min_risk_level = request.query_params.get("min_risk_level")
        min_case_count = _to_int(request.query_params.get("min_case_count"))
        limit = _to_int(request.query_params.get("limit"))

        if disease_id:
            clusters = clusters.filter(disease_id=disease_id)
        if min_risk_level:
            clusters = clusters.filter(risk_level__gte=min_risk_level)
        if min_case_count is not None:
            clusters = clusters.filter(case_count__gte=min_case_count)

        clusters = clusters.order_by("-risk_level", "-case_count", "-generated_at")
        if limit is not None and limit > 0:
            clusters = clusters[:limit]
        serializer = self.get_serializer(clusters, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=["get", "post"])
    def detect_dbscan(self, request):
        payload = request.data if request.method == "POST" else request.query_params
        disease_id = _to_int(payload.get("disease"))
        lookback_days = _to_int(payload.get("lookback_days"), 14)
        eps_km = _to_float(payload.get("eps_km"), 3.0)
        min_samples = _to_int(payload.get("min_samples"), 2)
        persist = _to_bool(payload.get("persist"))
        region_type = payload.get("region_type") or None

        if disease_id is not None and not Disease.objects.filter(id=disease_id).exists():
            return Response(
                {"error": "Disease not found."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            clusters = detect_dbscan_clusters(
                disease_id=disease_id,
                lookback_days=lookback_days,
                region_type=region_type,
                eps_km=eps_km if eps_km is not None else 3.0,
                min_samples=min_samples if min_samples is not None else 2,
            )
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        persisted_ids: list[int] = []
        if persist:
            persisted_ids = persist_dbscan_clusters(clusters=clusters)

        return Response(
            {
                "parameters": {
                    "disease_id": disease_id,
                    "lookback_days": lookback_days,
                    "region_type": region_type,
                    "eps_km": eps_km,
                    "min_samples": min_samples,
                    "persist": persist,
                },
                "cluster_count": len(clusters),
                "persisted_cluster_ids": persisted_ids,
                "clusters": serialize_dbscan_clusters(clusters),
            }
        )

    @action(detail=False, methods=["get", "post"])
    def detect_hdbscan(self, request):
        payload = request.data if request.method == "POST" else request.query_params
        disease_id = _to_int(payload.get("disease"))
        lookback_days = _to_int(payload.get("lookback_days"), 14)
        date_from = parse_date(payload.get("date_from")) if payload.get("date_from") else None
        date_to = parse_date(payload.get("date_to")) if payload.get("date_to") else None
        region_type = payload.get("region_type") or None
        min_cluster_size = _to_int(payload.get("min_cluster_size"), 3)
        min_samples = _to_int(payload.get("min_samples"))
        cluster_selection_method = payload.get("cluster_selection_method") or "eom"
        allow_single_cluster = _to_bool(payload.get("allow_single_cluster"))
        persist = _to_bool(payload.get("persist"))

        if payload.get("date_from") and date_from is None:
            return Response(
                {"error": "date_from must be in YYYY-MM-DD format."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if payload.get("date_to") and date_to is None:
            return Response(
                {"error": "date_to must be in YYYY-MM-DD format."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if disease_id is not None and not Disease.objects.filter(id=disease_id).exists():
            return Response(
                {"error": "Disease not found."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = detect_hdbscan_clusters(
                disease_id=disease_id,
                lookback_days=lookback_days,
                date_from=date_from,
                date_to=date_to,
                region_type=region_type,
                min_cluster_size=min_cluster_size if min_cluster_size is not None else 3,
                min_samples=min_samples,
                cluster_selection_method=cluster_selection_method,
                allow_single_cluster=allow_single_cluster,
            )
        except (ImportError, ModuleNotFoundError) as exc:
            return Response(
                {"error": f"HDBSCAN dependencies are not installed: {exc}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        persisted_ids: list[int] = []
        if persist:
            persisted_ids = persist_hdbscan_clusters(clusters=result.clusters)

        return Response(
            {
                "parameters": {
                    "disease_id": disease_id,
                    "lookback_days": lookback_days,
                    "date_from": date_from.isoformat() if date_from else None,
                    "date_to": date_to.isoformat() if date_to else None,
                    "region_type": region_type,
                    "min_cluster_size": min_cluster_size,
                    "min_samples": min_samples,
                    "cluster_selection_method": cluster_selection_method,
                    "allow_single_cluster": allow_single_cluster,
                    "persist": persist,
                },
                "cluster_count": len(result.clusters),
                "noise_count": result.noise_count,
                "persisted_ids": persisted_ids,
                "persisted_cluster_ids": persisted_ids,
                "clusters": serialize_hdbscan_clusters(result.clusters),
            }
        )


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
        reports = _active_alerts_queryset(self.get_queryset())
        disease_id = request.query_params.get("disease")
        alert_level = request.query_params.get("alert_level")
        status_filter = request.query_params.get("status")
        min_risk_score = _to_float(request.query_params.get("min_risk_score"))
        limit = _to_int(request.query_params.get("limit"))

        if disease_id:
            reports = reports.filter(disease_id=disease_id)
        if alert_level:
            reports = reports.filter(alert_level=alert_level)
        if status_filter:
            reports = reports.filter(status=status_filter)
        if min_risk_score is not None:
            reports = reports.filter(risk_score__gte=min_risk_score)

        reports = reports.order_by("-risk_score", "-generated_at")
        if limit is not None and limit > 0:
            reports = reports[:limit]
        serializer = self.get_serializer(reports, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=["get"])
    def dashboard_summary(self, request):
        active_reports = _active_alerts_queryset(self.get_queryset())
        active_clusters = _active_hotspots_queryset(GeoCluster.objects.all())
        active_visits = active_visits_queryset(Visit.objects.all())

        disease_id = request.query_params.get("disease")
        if disease_id:
            active_reports = active_reports.filter(disease_id=disease_id)
            active_clusters = active_clusters.filter(disease_id=disease_id)
            active_visits = active_visits.filter(disease_id=disease_id)

        top_alert = active_reports.order_by("-risk_score", "-generated_at").first()
        top_hotspot = active_clusters.order_by("-risk_level", "-case_count", "-generated_at").first()

        alerts_by_disease = list(
            active_reports.values("disease_id", "disease__name", "disease__disease_code")
            .annotate(
                alert_count=Count("id"),
                max_risk_score=Max("risk_score"),
            )
            .order_by("-alert_count", "-max_risk_score")
        )

        return Response(
            {
                "summary": {
                    "active_alert_count": active_reports.count(),
                    "critical_alert_count": active_reports.filter(alert_level="critical").count(),
                    "high_alert_count": active_reports.filter(alert_level="high").count(),
                    "medium_alert_count": active_reports.filter(alert_level="medium").count(),
                    "new_alert_count": active_reports.filter(status="new").count(),
                    "reviewed_alert_count": active_reports.filter(status="reviewed").count(),
                    "active_hotspot_count": active_clusters.count(),
                    "active_case_count": active_visits.count(),
                },
                "top_alert": ReportSerializer(top_alert).data if top_alert else None,
                "top_hotspot": GeoClusterSerializer(top_hotspot).data if top_hotspot else None,
                "alerts_by_disease": alerts_by_disease,
            }
        )

    @action(detail=False, methods=["get"])
    def detect_anomalies(self, request):
        disease_id = _to_int(request.query_params.get("disease"))
        lookback_days = _to_int(request.query_params.get("lookback_days"), 30)
        baseline_window_days = _to_int(request.query_params.get("baseline_window_days"), 7)
        region_type = request.query_params.get("region_type") or None

        if disease_id is not None and not Disease.objects.filter(id=disease_id).exists():
            return Response(
                {"error": "Disease not found."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            candidates = detect_temporal_anomalies(
                disease_id=disease_id,
                lookback_days=lookback_days if lookback_days is not None else 30,
                baseline_window_days=baseline_window_days if baseline_window_days is not None else 7,
                region_type=region_type,
            )
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "parameters": {
                    "disease_id": disease_id,
                    "lookback_days": lookback_days,
                    "baseline_window_days": baseline_window_days,
                    "region_type": region_type,
                },
                "candidate_count": len(candidates),
                "candidates": serialize_anomaly_candidates(candidates),
            }
        )

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
        report_kwargs = _build_generated_report_kwargs(
            disease=disease,
            analysis_period_start=analysis_period_start,
            analysis_period_end=analysis_period_end,
            region_type=region_type,
            trend=trend,
        )

        if request.method == "GET":
            serializer = ReportSerializer(Report(**report_kwargs))
            return Response(serializer.data)

        report = Report.objects.create(**report_kwargs)
        serializer = ReportSerializer(report)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
