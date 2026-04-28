from django.db import transaction

from core.models import Doctor, User


def create_linked_doctor_for_user(*, user: User, specialization: str, hospital) -> Doctor:
    if user.role != User.ROLE_DOCTOR:
        raise ValueError("Doctor profile can only be created for doctor users.")

    normalized_specialization = specialization.strip()
    if not normalized_specialization:
        raise ValueError("specialization is required for doctor users.")
    if hospital is None:
        raise ValueError("hospital is required for doctor users.")
    if Doctor.objects.filter(user=user).exists():
        raise ValueError("Doctor record already exists for this user.")

    return Doctor.objects.create(
        user=user,
        specialization=normalized_specialization,
        hospital=hospital,
    )


@transaction.atomic
def create_user_account(
    *,
    user_data: dict,
    password: str,
    specialization: str | None = None,
    hospital=None,
) -> User:
    user = User.objects.create_user(password=password, **user_data)

    if user.role == User.ROLE_DOCTOR:
        create_linked_doctor_for_user(
            user=user,
            specialization=specialization or "",
            hospital=hospital,
        )

    return user
