from django.utils.dateparse import parse_date
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from core.filters import GeoDataFilter, VisitFilter
from core.models import Disease, GeoCluster, GeoData, Visit
from core.permissions import IsAdminOnly, IsDoctorOrAdmin
from core.serializers import GeoClusterSerializer, GeoDataSerializer
from core.services.active_cases import (
    POINT_MODE_EXPOSURE,
    active_geodata_queryset,
    apply_geodata_point_mode,
    normalize_point_mode,
)
from core.services.dbscan_hotspots import (
    detect_dbscan_clusters,
    persist_dbscan_clusters,
    serialize_dbscan_clusters,
)
from core.services.hdbscan_hotspots import (
    detect_hdbscan_clusters,
    persist_hdbscan_clusters,
    serialize_hdbscan_clusters,
)
from core.views.utils import _active_hotspots_queryset, _to_bool, _to_float, _to_int


class GeoDataViewSet(viewsets.ModelViewSet):
    queryset = GeoData.objects.all()
    serializer_class = GeoDataSerializer
    permission_classes = [IsDoctorOrAdmin]

    @action(detail=False, methods=["get"])
    def active(self, request):
        try:
            point_mode = normalize_point_mode(request.query_params.get("point_mode"))
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        visits = VisitFilter(request.GET, queryset=Visit.objects.all()).qs
        geodata = active_geodata_queryset(
            GeoData.objects.all(),
            visit_queryset=visits,
            constrain_latest_to_queryset=False,
        )
        geodata = GeoDataFilter(request.GET, queryset=geodata).qs
        geodata = apply_geodata_point_mode(geodata, point_mode=point_mode).order_by("visit__diagnosis_date", "id")
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
        try:
            point_mode = normalize_point_mode(payload.get("point_mode") or POINT_MODE_EXPOSURE)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

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
                point_mode=point_mode,
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
                    "point_mode": point_mode,
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
        try:
            point_mode = normalize_point_mode(payload.get("point_mode") or POINT_MODE_EXPOSURE)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

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
                point_mode=point_mode,
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
                    "point_mode": point_mode,
                    "persist": persist,
                },
                "cluster_count": len(result.clusters),
                "noise_count": result.noise_count,
                "persisted_ids": persisted_ids,
                "persisted_cluster_ids": persisted_ids,
                "clusters": serialize_hdbscan_clusters(result.clusters),
            }
        )
