from rest_framework import serializers


def validate_number_range(value, *, minimum, maximum, field_name):
    if value is None:
        return value
    if value < minimum or value > maximum:
        raise serializers.ValidationError(
            f"{field_name} must be between {minimum} and {maximum}."
        )
    return value


def validate_coordinate_pair(attrs, *, latitude_field, longitude_field, required=False):
    latitude = attrs.get(latitude_field)
    longitude = attrs.get(longitude_field)

    if required and (latitude is None or longitude is None):
        raise serializers.ValidationError(
            {
                latitude_field: "Latitude is required.",
                longitude_field: "Longitude is required.",
            }
        )
    if (latitude is None) != (longitude is None):
        raise serializers.ValidationError(
            {
                latitude_field: "Latitude and longitude must be provided together.",
                longitude_field: "Latitude and longitude must be provided together.",
            }
        )
    if latitude is not None:
        validate_number_range(latitude, minimum=-90, maximum=90, field_name=latitude_field)
    if longitude is not None:
        validate_number_range(longitude, minimum=-180, maximum=180, field_name=longitude_field)


def validate_visit_measurements(attrs):
    weight = attrs.get("weight")
    height = attrs.get("height")
    if weight is not None:
        validate_number_range(weight, minimum=0.1, maximum=500, field_name="weight")
    if height is not None:
        validate_number_range(height, minimum=30, maximum=250, field_name="height")


def validate_diagnosis_not_before_birth(*, diagnosis_date, patient):
    if diagnosis_date is None or patient is None:
        return
    if patient.birth_date and diagnosis_date < patient.birth_date:
        raise serializers.ValidationError(
            {"diagnosis_date": "diagnosis_date cannot be earlier than patient birth_date."}
        )
