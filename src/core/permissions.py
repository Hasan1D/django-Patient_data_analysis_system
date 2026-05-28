from rest_framework.permissions import SAFE_METHODS, BasePermission

from .models import User


class IsDoctorOrAdmin(BasePermission):
    """
    Allows only doctors and admins.
    """

    def has_permission(self, request, view):
        return (
            request.user.is_authenticated
            and request.user.role in (User.ROLE_DOCTOR, User.ROLE_ADMIN)
        )


class IsAdminOnly(BasePermission):
    """
    Allows only admins.
    """

    def has_permission(self, request, view):
        return (
            request.user.is_authenticated
            and request.user.role == User.ROLE_ADMIN
        )


class IsAdminOrAuthenticatedReadOnly(BasePermission):
    """
    Allows authenticated users to read, while restricting writes to admins.
    """

    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False
        if request.method in SAFE_METHODS:
            return True
        return request.user.role == User.ROLE_ADMIN
