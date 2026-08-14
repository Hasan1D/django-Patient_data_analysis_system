from django.urls import re_path

from core.consumers import ActiveCasesMapConsumer


websocket_urlpatterns = [
    re_path(r"^ws/maps/active-cases/$", ActiveCasesMapConsumer.as_asgi()),
]
