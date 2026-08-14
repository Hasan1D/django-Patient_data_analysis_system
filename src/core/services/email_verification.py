from datetime import timedelta
from secrets import randbelow

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core.mail import send_mail
from django.utils import timezone

from ..models import EmailVerificationCode
from .account_service import mark_user_email_verified


DEFAULT_CODE_EXPIRY_MINUTES = 10
DEFAULT_CODE_LENGTH = 6
DEFAULT_MAX_ATTEMPTS = 5


def _setting_int(name: str, default: int) -> int:
    value = getattr(settings, name, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _generate_numeric_code(length: int) -> str:
    return "".join(str(randbelow(10)) for _ in range(length))


def send_email_verification_code(user) -> EmailVerificationCode:
    code_length = _setting_int("EMAIL_VERIFICATION_CODE_LENGTH", DEFAULT_CODE_LENGTH)
    expiry_minutes = _setting_int(
        "EMAIL_VERIFICATION_CODE_EXPIRY_MINUTES",
        DEFAULT_CODE_EXPIRY_MINUTES,
    )
    code = _generate_numeric_code(code_length)
    now = timezone.now()

    verification, _ = EmailVerificationCode.objects.update_or_create(
        user=user,
        defaults={
            "code_hash": make_password(code),
            "created_at": now,
            "expires_at": now + timedelta(minutes=expiry_minutes),
            "attempts": 0,
        },
    )

    send_mail(
        subject="Verify your account",
        message=(
            f"Your account verification code is {code}.\n"
            f"This code expires in {expiry_minutes} minutes."
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=False,
    )
    return verification


def verify_email_code(user, code: str) -> bool:
    try:
        verification = user.email_verification_code
    except EmailVerificationCode.DoesNotExist:
        raise ValueError("No verification code was found for this account.")

    if timezone.now() > verification.expires_at:
        verification.delete()
        raise ValueError("Verification code has expired. Please request a new code.")

    max_attempts = _setting_int(
        "EMAIL_VERIFICATION_MAX_ATTEMPTS",
        DEFAULT_MAX_ATTEMPTS,
    )
    if verification.attempts >= max_attempts:
        verification.delete()
        raise ValueError("Too many failed attempts. Please request a new code.")

    if not check_password(str(code).strip(), verification.code_hash):
        verification.attempts += 1
        verification.save(update_fields=("attempts",))
        raise ValueError("Invalid verification code.")

    mark_user_email_verified(user)
    verification.delete()
    return True
