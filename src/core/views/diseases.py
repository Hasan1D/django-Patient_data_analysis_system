from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from core.models import Disease, LabTest
from core.permissions import IsDoctorOrAdmin
from core.serializers import DiseaseSerializer, LabTestSerializer


class DiseaseViewSet(viewsets.ModelViewSet):
    queryset = Disease.objects.all()
    serializer_class = DiseaseSerializer
    permission_classes = [IsAuthenticated]


class LabTestViewSet(viewsets.ModelViewSet):
    queryset = LabTest.objects.all()
    serializer_class = LabTestSerializer
    permission_classes = [IsDoctorOrAdmin]
