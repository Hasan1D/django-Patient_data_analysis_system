from math import isfinite

from django.db import transaction

from core.models import (
    Allergy,
    GeoData,
    LabTest,
    MedicalHistory,
    Patient,
    SurgicalHistory,
    Vaccine,
    Visit,
    chronicDisease,
)


def ensure_medical_history(*, patient: Patient) -> MedicalHistory:
    history, _ = MedicalHistory.objects.get_or_create(patient=patient)
    return history


def _is_valid_coordinate(value, *, minimum: float, maximum: float) -> bool:
    if value is None:
        return False
    try:
        coordinate = float(value)
    except (TypeError, ValueError):
        return False
    return isfinite(coordinate) and minimum <= coordinate <= maximum


def _is_valid_coordinate_pair(*, latitude, longitude) -> bool:
    return _is_valid_coordinate(latitude, minimum=-90, maximum=90) and _is_valid_coordinate(
        longitude,
        minimum=-180,
        maximum=180,
    )


def _create_geodata_if_missing(
    *,
    visit: Visit,
    region_type: str,
    latitude: float,
    longitude: float,
) -> GeoData:
    existing_geodata = GeoData.objects.filter(
        visit=visit,
        region_type=region_type,
    ).first()
    if existing_geodata is not None:
        return existing_geodata

    return GeoData.objects.create(
        patient=visit.patient,
        visit=visit,
        latitude=latitude,
        longitude=longitude,
        region_type=region_type,
    )


def create_geodata_from_patient_coordinates(*, visit: Visit) -> list[GeoData]:
    patient = visit.patient
    geodata_records = []

    if _is_valid_coordinate_pair(
        latitude=patient.residence_lat,
        longitude=patient.residence_long,
    ):
        geodata_records.append(
            _create_geodata_if_missing(
                visit=visit,
                region_type=GeoData.REGION_HOME,
                latitude=patient.residence_lat,
                longitude=patient.residence_long,
            )
        )

    if _is_valid_coordinate_pair(
        latitude=patient.work_lat,
        longitude=patient.work_long,
    ):
        geodata_records.append(
            _create_geodata_if_missing(
                visit=visit,
                region_type=GeoData.REGION_WORK,
                latitude=patient.work_lat,
                longitude=patient.work_long,
            )
        )

    return geodata_records


@transaction.atomic
def create_visit_for_patient(*, patient: Patient, visit_data: dict) -> Visit:
    visit = Visit.objects.create(patient=patient, **visit_data)
    create_geodata_from_patient_coordinates(visit=visit)
    return visit


def create_lab_test_for_visit(*, visit: Visit, lab_test_data: dict) -> LabTest:
    return LabTest.objects.create(visit=visit, **lab_test_data)


def create_allergy_for_patient(*, patient: Patient, allergy_data: dict) -> Allergy:
    history = ensure_medical_history(patient=patient)
    return Allergy.objects.create(history=history, **allergy_data)


def create_chronic_disease_for_patient(
    *,
    patient: Patient,
    chronic_disease_data: dict,
) -> chronicDisease:
    history = ensure_medical_history(patient=patient)
    return chronicDisease.objects.create(history=history, **chronic_disease_data)


def create_vaccine_for_patient(*, patient: Patient, vaccine_data: dict) -> Vaccine:
    history = ensure_medical_history(patient=patient)
    return Vaccine.objects.create(history=history, **vaccine_data)


def create_surgical_history_for_patient(
    *,
    patient: Patient,
    surgical_history_data: dict,
) -> SurgicalHistory:
    history = ensure_medical_history(patient=patient)
    return SurgicalHistory.objects.create(history=history, **surgical_history_data)
