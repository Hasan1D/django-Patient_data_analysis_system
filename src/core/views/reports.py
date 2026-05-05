from datetime import timedelta

from django.db.models import Count, Max
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from core.models import Disease, GeoCluster, Report, Visit
from core.permissions import IsAdminOnly
from core.serializers import GeoClusterSerializer, ReportSerializer
from core.services.active_cases import active_visits_queryset
from core.services.anomaly_detection import detect_temporal_anomalies, serialize_anomaly_candidates
from core.services.trend import build_trend_snapshot
from core.views.utils import (
    _active_alerts_queryset,
    _active_hotspots_queryset,
    _build_generated_report_kwargs,
    _resolve_report_dates,
    _to_float,
    _to_int,
)


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
