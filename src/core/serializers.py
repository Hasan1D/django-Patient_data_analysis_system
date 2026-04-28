from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers
from .models import (
    Allergy,
    Disease,
    Doctor,
    GeoCluster,
    GeoData,
    Hospital,
    LabTest,
    MedicalHistory,
    Patient,
    Report,
    SurgicalHistory,
    User,
    Vaccine,
    Visit,
    chronicDisease,
)


class URLLinkedModelSerializer(serializers.ModelSerializer):
    url_linked_fields: tuple[str, ...] = ()

    def validate(self, attrs):
        errors = {}
        initial_data = getattr(self, "initial_data", {})
        for field_name in self.url_linked_fields:
            if field_name in initial_data:
                errors[field_name] = "This field is taken from the URL."
        if errors:
            raise serializers.ValidationError(errors)

        return super().validate(attrs)


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = (
            "id",
            "username",
            "real_name",
            "phon_number",
            "email",
            "role",
            "is_active",
            "is_staff",
        )
        read_only_fields = ("id", "is_staff")


class UserRegistrationSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    password_confirm = serializers.CharField(write_only=True, trim_whitespace=False)

    class Meta:
        model = User
        fields = (
            "id",
            "username",
            "real_name",
            "phon_number",
            "email",
            "password",
            "password_confirm",
        )
        read_only_fields = ("id",)

    def validate_email(self, value):
        normalized_value = value.strip().lower()
        if not normalized_value:
            raise serializers.ValidationError("Email is required.")
        if User.objects.filter(email__iexact=normalized_value).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return normalized_value

    def validate(self, attrs):
        if attrs["password"] != attrs["password_confirm"]:
            raise serializers.ValidationError({"password_confirm": "Passwords do not match."})
        validate_password(attrs["password"])
        return attrs

    def create(self, validated_data):
        validated_data.pop("password_confirm")
        password = validated_data.pop("password")
        return User.objects.create_user(
            password=password,
            role=User.ROLE_DOCTOR,
            is_active=False,
            **validated_data,
        )


class EmailVerificationSerializer(serializers.Serializer):
    email = serializers.EmailField()
    code = serializers.RegexField(regex=r"^\d+$", max_length=12)

    def validate_email(self, value):
        return value.strip().lower()

    def validate(self, attrs):
        users = list(User.objects.filter(email__iexact=attrs["email"])[:2])
        if not users:
            raise serializers.ValidationError({"email": "No account was found for this email."})
        if len(users) > 1:
            raise serializers.ValidationError({"email": "More than one account uses this email."})
        self.user = users[0]
        return attrs


class ResendEmailVerificationSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def validate_email(self, value):
        normalized_value = value.strip().lower()
        users = list(User.objects.filter(email__iexact=normalized_value)[:2])
        if not users:
            raise serializers.ValidationError("No account was found for this email.")
        if len(users) > 1:
            raise serializers.ValidationError("More than one account uses this email.")
        user = users[0]
        if user.is_active and not hasattr(user, "email_verification_code"):
            raise serializers.ValidationError("This account is already active.")
        self.user = user
        return normalized_value


class PatientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Patient
        fields = "__all__"


class DoctorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Doctor
        fields = "__all__"


class HospitalSerializer(serializers.ModelSerializer):
    class Meta:
        model = Hospital
        fields = "__all__"


class DiseaseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Disease
        fields = "__all__"

    def validate_disease_code(self, value):
        normalized_value = value.strip().upper()
        if not normalized_value:
            raise serializers.ValidationError("disease_code cannot be blank.")
        return normalized_value

    def validate_name(self, value):
        normalized_value = value.strip()
        if not normalized_value:
            raise serializers.ValidationError("name cannot be blank.")
        return normalized_value

    def validate_risk_level(self, value):
        if value < 0:
            raise serializers.ValidationError("risk_level cannot be negative.")
        return value

    def validate_infection_score(self, value):
        if value < 0:
            raise serializers.ValidationError("infection_score cannot be negative.")
        return value


class VisitSerializer(serializers.ModelSerializer):
    class Meta:
        model = Visit
        fields = "__all__"


class PatientVisitCreateSerializer(URLLinkedModelSerializer):
    url_linked_fields = ("patient",)

    class Meta:
        model = Visit
        fields = (
            "doctor",
            "disease",
            "diagnose",
            "diagnosis_date",
            "status",
            "weight",
            "height",
            "marital_status",
        )


class GeoDataSerializer(serializers.ModelSerializer):
    class Meta:
        model = GeoData
        fields = "__all__"

    def validate(self, attrs):
        patient = attrs.get("patient", getattr(self.instance, "patient", None))
        visit = attrs.get("visit", getattr(self.instance, "visit", None))

        if patient is not None and visit is not None and patient.id != visit.patient_id:
            raise serializers.ValidationError(
                {"patient": "patient must match visit.patient."}
            )

        return attrs


class GeoClusterSerializer(serializers.ModelSerializer):
    disease_name = serializers.CharField(source="disease.name", read_only=True)
    disease_code = serializers.CharField(source="disease.disease_code", read_only=True)

    class Meta:
        model = GeoCluster
        fields = "__all__"
        read_only_fields = ("generated_at", "disease_name", "disease_code")


class ReportSerializer(serializers.ModelSerializer):
    disease_name = serializers.CharField(source="disease.name", read_only=True)
    disease_code = serializers.CharField(source="disease.disease_code", read_only=True)
    trigger_visit_diagnosis_date = serializers.DateField(
        source="trigger_visit.diagnosis_date",
        read_only=True,
    )

    class Meta:
        model = Report
        fields = "__all__"
        read_only_fields = (
            "generated_at",
            "disease_name",
            "disease_code",
            "trigger_visit_diagnosis_date",
        )

    def validate(self, attrs):
        analysis_period_start = attrs.get(
            "analysis_period_start",
            getattr(self.instance, "analysis_period_start", None),
        )
        analysis_period_end = attrs.get(
            "analysis_period_end",
            getattr(self.instance, "analysis_period_end", None),
        )

        if (
            analysis_period_start is not None
            and analysis_period_end is not None
            and analysis_period_start > analysis_period_end
        ):
            raise serializers.ValidationError(
                {"analysis_period_end": "analysis_period_end cannot be earlier than analysis_period_start."}
            )

        disease = attrs.get("disease", getattr(self.instance, "disease", None))
        trigger_visit = attrs.get("trigger_visit", getattr(self.instance, "trigger_visit", None))
        if trigger_visit is not None and disease is not None and trigger_visit.disease_id != disease.id:
            raise serializers.ValidationError(
                {"trigger_visit": "trigger_visit must belong to the same disease as the report."}
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
