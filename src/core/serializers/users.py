from rest_framework import serializers

from core.models import Doctor, Hospital

from .validation import validate_coordinate_pair


class DoctorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Doctor
        fields = "__all__"


class DoctorMeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Doctor
        fields = "__all__"
        read_only_fields = ("id", "user", "hospital")


class HospitalSerializer(serializers.ModelSerializer):
    class Meta:
        model = Hospital
        fields = "__all__"

    def validate(self, attrs):
        values = {
            "hospital_lat": attrs.get("hospital_lat", getattr(self.instance, "hospital_lat", None)),
            "hospital_long": attrs.get("hospital_long", getattr(self.instance, "hospital_long", None)),
        }
        validate_coordinate_pair(
            values,
            latitude_field="hospital_lat",
            longitude_field="hospital_long",
            required=True,
        )
        return super().validate(attrs)


class RegistrationHospitalSerializer(serializers.ModelSerializer):
    class Meta:
        model = Hospital
        fields = (
            "id",
            "name",
            "city",
            "location",
        )
