from rest_framework import serializers

from core.models import Report


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
