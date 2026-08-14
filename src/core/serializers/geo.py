from rest_framework import serializers

from core.models import GeoCluster, GeoData, Visit

from .validation import validate_coordinate_pair


class MapCasesQuerySerializer(serializers.Serializer):
    disease_code = serializers.CharField(required=False, trim_whitespace=True)
    disease_type = serializers.CharField(required=False, trim_whitespace=True)
    start_date = serializers.DateField(required=False)
    end_date = serializers.DateField(required=False)
    region_type = serializers.ChoiceField(
        choices=GeoData.REGION_TYPE_CHOICES,
        required=False,
    )
    status = serializers.ChoiceField(
        choices=Visit.STATUS_CHOICES,
        required=False,
    )
    limit = serializers.IntegerField(
        required=False,
        min_value=1,
        max_value=10000,
        default=5000,
    )

    def validate_disease_code(self, value):
        normalized_value = value.strip().upper()
        if not normalized_value:
            raise serializers.ValidationError("disease_code cannot be blank.")
        return normalized_value

    def validate_disease_type(self, value):
        normalized_value = value.strip()
        if not normalized_value:
            raise serializers.ValidationError("disease_type cannot be blank.")
        return normalized_value

    def validate(self, attrs):
        start_date = attrs.get("start_date")
        end_date = attrs.get("end_date")
        if start_date and end_date and start_date > end_date:
            raise serializers.ValidationError(
                {"end_date": "end_date cannot be earlier than start_date."}
            )
        return attrs


class GeoDataSerializer(serializers.ModelSerializer):
    class Meta:
        model = GeoData
        fields = "__all__"

    def validate(self, attrs):
        values = {
            "latitude": attrs.get("latitude", getattr(self.instance, "latitude", None)),
            "longitude": attrs.get("longitude", getattr(self.instance, "longitude", None)),
        }
        validate_coordinate_pair(
            values,
            latitude_field="latitude",
            longitude_field="longitude",
            required=True,
        )
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

    def validate(self, attrs):
        values = {
            "center_lat": attrs.get("center_lat", getattr(self.instance, "center_lat", None)),
            "center_long": attrs.get("center_long", getattr(self.instance, "center_long", None)),
        }
        validate_coordinate_pair(
            values,
            latitude_field="center_lat",
            longitude_field="center_long",
            required=True,
        )
        radius = attrs.get("radius", getattr(self.instance, "radius", None))
        if radius is not None and radius < 0:
            raise serializers.ValidationError({"radius": "radius cannot be negative."})
        case_count = attrs.get("case_count", getattr(self.instance, "case_count", None))
        if case_count is not None and case_count < 0:
            raise serializers.ValidationError({"case_count": "case_count cannot be negative."})
        risk_level = attrs.get("risk_level", getattr(self.instance, "risk_level", None))
        if risk_level is not None and (risk_level < 0 or risk_level > 5):
            raise serializers.ValidationError({"risk_level": "risk_level must be between 0 and 5."})
        return super().validate(attrs)
