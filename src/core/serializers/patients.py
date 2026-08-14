from datetime import date

from rest_framework import serializers

from core.models import Patient

from .validation import validate_coordinate_pair


class PatientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Patient
        fields = "__all__"

    def validate_birth_date(self, value):
        if value > date.today():
            raise serializers.ValidationError("birth_date cannot be in the future.")
        return value

    def validate_name(self, value):
        normalized_value = value.strip()
        if not normalized_value:
            raise serializers.ValidationError("name cannot be blank.")
        return normalized_value

    def validate_national_number(self, value):
        normalized_value = value.strip()
        if not normalized_value:
            raise serializers.ValidationError("national_number cannot be blank.")
        return normalized_value

    def validate(self, attrs):
        values = {
            "residence_lat": attrs.get("residence_lat", getattr(self.instance, "residence_lat", None)),
            "residence_long": attrs.get("residence_long", getattr(self.instance, "residence_long", None)),
            "work_lat": attrs.get("work_lat", getattr(self.instance, "work_lat", None)),
            "work_long": attrs.get("work_long", getattr(self.instance, "work_long", None)),
        }
        validate_coordinate_pair(
            values,
            latitude_field="residence_lat",
            longitude_field="residence_long",
        )
        validate_coordinate_pair(
            values,
            latitude_field="work_lat",
            longitude_field="work_long",
        )
        return super().validate(attrs)
