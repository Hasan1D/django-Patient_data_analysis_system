from rest_framework import viewsets

from core.models import Disease, LabTest
from core.permissions import IsAdminOrAuthenticatedReadOnly, IsDoctorOrAdmin
from core.serializers import DiseaseSerializer, LabTestSerializer
from core.services.access_control import labtests_visible_to_user


class DiseaseViewSet(viewsets.ModelViewSet):
    queryset = Disease.objects.all()
    serializer_class = DiseaseSerializer
    permission_classes = [IsAdminOrAuthenticatedReadOnly]


class LabTestViewSet(viewsets.ModelViewSet):
    queryset = LabTest.objects.select_related(
        "visit__patient",
        "visit__doctor__hospital",
        "visit__disease",
    ).all()
    serializer_class = LabTestSerializer
    permission_classes = [IsDoctorOrAdmin]

    def get_queryset(self):
        return labtests_visible_to_user(
            super().get_queryset(),
            self.request.user,
        ).order_by("-test_date", "-id")
