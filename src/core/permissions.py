from rest_framework.permissions import BasePermission


class IsDoctorOrAdmin(BasePermission):
    """
    يسمح فقط للطبيب أو المدير
    """
    def has_permission(self, request, view):
        return (
            request.user.is_authenticated and
            request.user.role in ['doctor', 'admin']
        )


class IsAdminOnly(BasePermission):
    """
    يسمح فقط للمدير
    """
    def has_permission(self, request, view):
        return (
            request.user.is_authenticated and
            request.user.role == 'admin'
        )