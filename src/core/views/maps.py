from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import IsDoctorOrAdmin
from core.serializers import MapCasesQuerySerializer
from core.services.map_cases import (
    MapCaseFilters,
    active_map_cases_queryset,
    historical_map_cases_queryset,
    map_cases_to_feature_collection,
)


def _query_data_with_date_aliases(query_params):
    data = query_params.copy()
    if "date_from" in data and "start_date" not in data:
        data["start_date"] = data["date_from"]
    if "date_to" in data and "end_date" not in data:
        data["end_date"] = data["date_to"]
    return data


def _validated_filters(request) -> MapCaseFilters:
    serializer = MapCasesQuerySerializer(data=_query_data_with_date_aliases(request.query_params))
    serializer.is_valid(raise_exception=True)
    return MapCaseFilters(**serializer.validated_data)


class HistoricalMapCasesView(APIView):
    permission_classes = [IsDoctorOrAdmin]

    def get(self, request):
        filters = _validated_filters(request)
        geodata_records = historical_map_cases_queryset(filters)
        return Response(map_cases_to_feature_collection(geodata_records))


class ActiveMapCasesView(APIView):
    permission_classes = [IsDoctorOrAdmin]

    def get(self, request):
        filters = _validated_filters(request)
        geodata_records = active_map_cases_queryset(filters)
        return Response(map_cases_to_feature_collection(geodata_records))
