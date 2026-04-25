from rest_framework.permissions import BasePermission

from .models import User


class IsDoctorOrAdmin(BasePermission):
    """
    يسمح فقط للطبيب أو المدير
    """
    def has_permission(self, request, view):
        return (
            request.user.is_authenticated and
            request.user.role in (User.ROLE_DOCTOR, User.ROLE_ADMIN)
        )


class IsAdminOnly(BasePermission):
    """
    يسمح فقط للمدير
    """
    def has_permission(self, request, view):
        return (
            request.user.is_authenticated and
            request.user.role == User.ROLE_ADMIN
        )
