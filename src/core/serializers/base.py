from rest_framework import serializers


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
