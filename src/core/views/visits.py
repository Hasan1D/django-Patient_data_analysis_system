from dataclasses import asdict, replace

from django.db.models import Count
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from core.filters import GeoDataFilter, VisitFilter
from core.models import GeoData, User, Visit
from core.permissions import IsDoctorOrAdmin
from core.serializers import LabTestSerializer, VisitLabTestCreateSerializer, VisitSerializer
from core.services import VisitOutbreakContext
from core.services.alert_policies import get_policy_for_disease
from core.services.cluster_service import create_cluster_from_analysis
from core.services.outbreak_engine import evaluate_visit_outbreak
from core.services.report_service import create_report_from_analysis
from core.services.risk_prediction import predict_visit_risk, serialize_risk_prediction
from core.services.workflow_service import create_lab_test_for_visit
from core.views.utils import _to_bool


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
            .annotate(total_cases=Count("visit", distinct=True))
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
            .annotate(total_cases=Count("visit", distinct=True))
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

    @action(detail=True, methods=["get", "post"])
    def evaluate_exposure_outbreak(self, request, pk=None):
        visit = self.get_object()
        payload = request.data if request.method == "POST" else request.query_params
        save_report = _to_bool(payload.get("save_report"))
        save_cluster = _to_bool(payload.get("save_cluster"))

        if (save_report or save_cluster) and request.user.role != User.ROLE_ADMIN:
            return Response(
                {"error": "Only admin can persist outbreak outputs."},
                status=status.HTTP_403_FORBIDDEN,
            )

        geodata_records = list(GeoData.objects.filter(visit=visit).order_by("id"))
        if not geodata_records:
            return Response(
                {"error": "No GeoData records were found for this visit."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        results = []
        for geodata in geodata_records:
            policy = get_policy_for_disease(
                disease_code=visit.disease.disease_code,
                risk_level=visit.disease.risk_level,
                infection_score=visit.disease.infection_score,
                high_priority=visit.disease.high_priority,
                rare_disease=visit.disease.rare_disease,
                policy_profile=visit.disease.policy_profile,
            )
            policy = replace(policy, region_type=geodata.region_type)
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

            results.append(
                {
                    "geodata_id": geodata.id,
                    "region_type": geodata.region_type,
                    "latitude": geodata.latitude,
                    "longitude": geodata.longitude,
                    "policy": asdict(policy),
                    "analysis": asdict(analysis),
                    "persistence": persistence,
                }
            )

        return Response(
            {
                "visit_id": visit.id,
                "disease": {
                    "id": visit.disease_id,
                    "code": visit.disease.disease_code,
                    "name": visit.disease.name,
                },
                "results": results,
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
