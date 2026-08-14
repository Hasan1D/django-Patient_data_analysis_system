from rest_framework import serializers

from core.models import (
    Allergy,
    MedicalHistory,
    SurgicalHistory,
    Vaccine,
    chronicDisease,
)

from .base import URLLinkedModelSerializer


class MedicalHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = MedicalHistory
        fields = "__all__"


class VaccineSerializer(serializers.ModelSerializer):
    class Meta:
        model = Vaccine
        fields = "__all__"


class PatientVaccineCreateSerializer(URLLinkedModelSerializer):
    url_linked_fields = ("history",)

    class Meta:
        model = Vaccine
        fields = (
            "vaccine_name",
            "date_administered",
        )


class AllergySerializer(serializers.ModelSerializer):
    class Meta:
        model = Allergy
        fields = "__all__"


class PatientAllergyCreateSerializer(URLLinkedModelSerializer):
    url_linked_fields = ("history",)

    class Meta:
        model = Allergy
        fields = (
            "allergy_name",
            "severity_level",
        )


class ChronicDiseaseSerializer(serializers.ModelSerializer):
    class Meta:
        model = chronicDisease
        fields = "__all__"


class PatientChronicDiseaseCreateSerializer(URLLinkedModelSerializer):
    url_linked_fields = ("history",)

    class Meta:
        model = chronicDisease
        fields = (
            "disease_name",
            "diagnosis_date",
        )


class SurgicalHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = SurgicalHistory
        fields = "__all__"


class PatientSurgicalHistoryCreateSerializer(URLLinkedModelSerializer):
    url_linked_fields = ("history",)

    class Meta:
        model = SurgicalHistory
        fields = (
            "surgery_description",
            "surgery_date",
            "has_metal_plates",
        )
