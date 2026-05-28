from rest_framework import serializers

from core.models import Disease, Doctor, LabTest, User, Visit

from .base import URLLinkedModelSerializer
from .validation import validate_diagnosis_not_before_birth, validate_visit_measurements


class VisitDoctorInfoSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", read_only=True)
    real_name = serializers.CharField(source="user.real_name", read_only=True)
    hospital_name = serializers.CharField(source="hospital.name", read_only=True)

    class Meta:
        model = Doctor
        fields = (
            "id",
            "username",
            "real_name",
            "specialization",
            "hospital",
            "hospital_name",
        )


class VisitSerializer(serializers.ModelSerializer):
    doctor_info = VisitDoctorInfoSerializer(source="doctor", read_only=True)

    class Meta:
        model = Visit
        fields = "__all__"

    def validate(self, attrs):
        attrs = super().validate(attrs)
        validate_visit_measurements(attrs)
        patient = attrs.get("patient", getattr(self.instance, "patient", None))
        diagnosis_date = attrs.get(
            "diagnosis_date",
            getattr(self.instance, "diagnosis_date", None),
        )
        validate_diagnosis_not_before_birth(
            diagnosis_date=diagnosis_date,
            patient=patient,
        )
        return attrs


class PatientVisitCreateSerializer(URLLinkedModelSerializer):
    disease_code = serializers.CharField(write_only=True, trim_whitespace=True)
    doctor_id = serializers.IntegerField(required=False, write_only=True)
    url_linked_fields = ("patient", "disease", "doctor")

    class Meta:
        model = Visit
        fields = (
            "disease_code",
            "doctor_id",
            "diagnose",
            "diagnosis_date",
            "status",
            "weight",
            "height",
            "marital_status",
        )

    def validate_disease_code(self, value):
        normalized_value = value.strip().upper()
        if not normalized_value:
            raise serializers.ValidationError("disease_code cannot be blank.")
        return normalized_value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        errors = {}
        validate_visit_measurements(attrs)

        disease_code = attrs.pop("disease_code")
        try:
            attrs["disease"] = Disease.objects.get(disease_code=disease_code)
        except Disease.DoesNotExist:
            errors["disease_code"] = "Disease with this code does not exist."

        request = self.context.get("request")
        user = getattr(request, "user", None)
        doctor_id = attrs.pop("doctor_id", None)

        if user is None or not user.is_authenticated:
            errors["doctor"] = "Authentication is required to create a visit."
        elif user.role == User.ROLE_DOCTOR:
            try:
                attrs["doctor"] = Doctor.objects.get(user=user)
            except Doctor.DoesNotExist:
                errors["doctor"] = "No Doctor record is linked to the logged-in user."
        elif user.role == User.ROLE_ADMIN:
            if doctor_id is None:
                errors["doctor_id"] = "doctor_id is required when an admin creates a visit."
            else:
                try:
                    attrs["doctor"] = Doctor.objects.get(id=doctor_id)
                except Doctor.DoesNotExist:
                    errors["doctor_id"] = "Doctor with this id does not exist."
        else:
            errors["doctor"] = "Only doctors or admins can create visits."

        if errors:
            raise serializers.ValidationError(errors)

        patient = self.context.get("patient")
        validate_diagnosis_not_before_birth(
            diagnosis_date=attrs.get("diagnosis_date"),
            patient=patient,
        )

        return attrs


class LabTestSerializer(serializers.ModelSerializer):
    class Meta:
        model = LabTest
        fields = "__all__"


class VisitLabTestCreateSerializer(URLLinkedModelSerializer):
    url_linked_fields = ("visit",)

    class Meta:
        model = LabTest
        fields = (
            "test_code",
            "test_name",
            "result",
            "test_date",
            "notes",
        )
