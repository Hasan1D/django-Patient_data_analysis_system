from rest_framework import serializers

from core.models import Disease


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
        if value < 0 or value > 5:
            raise serializers.ValidationError("risk_level must be between 0 and 5.")
        return value

    def validate_infection_score(self, value):
        if value < 0:
            raise serializers.ValidationError("infection_score cannot be negative.")
        return value
