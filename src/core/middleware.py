import logging

from django.conf import settings

from core.models import AuditLog


logger = logging.getLogger(__name__)


class AuditLogMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        self._log_request(request, response)
        return response

    def _log_request(self, request, response):
        if not getattr(settings, "AUDIT_LOG_ENABLED", True):
            return
        if not request.path.startswith("/api/"):
            return

        user = getattr(request, "user", None)
        authenticated = bool(getattr(user, "is_authenticated", False))
        forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
        ip_address = forwarded_for.split(",", 1)[0].strip() or request.META.get("REMOTE_ADDR")

        try:
            AuditLog.objects.create(
                user=user if authenticated else None,
                username=getattr(user, "username", "") if authenticated else "",
                method=request.method,
                path=request.path[:512],
                status_code=getattr(response, "status_code", 0),
                ip_address=ip_address or None,
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
            )
        except Exception:  # pragma: no cover
            logger.exception("Failed to write audit log for %s %s", request.method, request.path)
