from django.db import transaction

from core.models import Doctor, User


def update_user_activation_state(user: User) -> User:
    should_be_active = user.email_verified and user.admin_approved
    if user.is_active != should_be_active:
        user.is_active = should_be_active
        user.save(update_fields=("is_active",))
    return user


def mark_user_email_verified(user: User) -> User:
    user.email_verified = True
    should_be_active = user.admin_approved
    update_fields = ["email_verified"]
    if user.is_active != should_be_active:
        user.is_active = should_be_active
        update_fields.append("is_active")
    user.save(update_fields=update_fields)
    return user


def approve_user_account(user: User) -> User:
    user.admin_approved = True
    should_be_active = user.email_verified
    update_fields = ["admin_approved"]
    if user.is_active != should_be_active:
        user.is_active = should_be_active
        update_fields.append("is_active")
    user.save(update_fields=update_fields)
    return user


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
