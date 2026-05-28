from __future__ import annotations

from django.db.models import Q, QuerySet

from core.models import Doctor, GeoData, LabTest, Patient, User, Visit


def is_admin(user) -> bool:
    return bool(
        getattr(user, "is_authenticated", False)
        and getattr(user, "role", None) == User.ROLE_ADMIN
    )


def doctor_hospital_id_for_user(user) -> int | None:
    if not getattr(user, "is_authenticated", False):
        return None
    if getattr(user, "role", None) != User.ROLE_DOCTOR:
        return None

    return (
        Doctor.objects.filter(user=user)
        .values_list("hospital_id", flat=True)
        .first()
    )


def doctors_visible_to_user(queryset: QuerySet[Doctor], user) -> QuerySet[Doctor]:
    if is_admin(user):
        return queryset

    hospital_id = doctor_hospital_id_for_user(user)
    if hospital_id is None:
        return queryset.none()
    return queryset.filter(hospital_id=hospital_id)


def hospitals_visible_to_user(queryset: QuerySet, user) -> QuerySet:
    if is_admin(user):
        return queryset

    hospital_id = doctor_hospital_id_for_user(user)
    if hospital_id is None:
        return queryset.none()
    return queryset.filter(id=hospital_id)


def patients_visible_to_user(queryset: QuerySet[Patient], user) -> QuerySet[Patient]:
    if is_admin(user):
        return queryset

    hospital_id = doctor_hospital_id_for_user(user)
    if hospital_id is None:
        return queryset.none()

    return queryset.filter(
        Q(visit__doctor__hospital_id=hospital_id)
        | Q(visit__isnull=True)
    ).distinct()


def visits_visible_to_user(queryset: QuerySet[Visit], user) -> QuerySet[Visit]:
    if is_admin(user):
        return queryset

    hospital_id = doctor_hospital_id_for_user(user)
    if hospital_id is None:
        return queryset.none()
    return queryset.filter(doctor__hospital_id=hospital_id)


def geodata_visible_to_user(queryset: QuerySet[GeoData], user) -> QuerySet[GeoData]:
    if is_admin(user):
        return queryset

    hospital_id = doctor_hospital_id_for_user(user)
    if hospital_id is None:
        return queryset.none()
    return queryset.filter(visit__doctor__hospital_id=hospital_id)


def labtests_visible_to_user(queryset: QuerySet[LabTest], user) -> QuerySet[LabTest]:
    if is_admin(user):
        return queryset

    hospital_id = doctor_hospital_id_for_user(user)
    if hospital_id is None:
        return queryset.none()
    return queryset.filter(visit__doctor__hospital_id=hospital_id)
