from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from core.models import Patient
from core.permissions import IsDoctorOrAdmin
from core.serializers import (
    AllergySerializer,
    ChronicDiseaseSerializer,
    PatientAllergyCreateSerializer,
    PatientChronicDiseaseCreateSerializer,
    PatientSerializer,
    PatientSurgicalHistoryCreateSerializer,
    PatientVaccineCreateSerializer,
    PatientVisitCreateSerializer,
    SurgicalHistorySerializer,
    VaccineSerializer,
    VisitSerializer,
)
from core.services.workflow_service import (
    create_allergy_for_patient,
    create_chronic_disease_for_patient,
    create_surgical_history_for_patient,
    create_vaccine_for_patient,
    create_visit_for_patient,
)


class PatientViewSet(viewsets.ModelViewSet):
    queryset = Patient.objects.all()
    serializer_class = PatientSerializer
    permission_classes = [IsDoctorOrAdmin]

    @action(detail=True, methods=["post"], url_path="visits")
    def visits(self, request, pk=None):
        patient = self.get_object()
        serializer = PatientVisitCreateSerializer(
            data=request.data,
            context={"request": request},
        )
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
