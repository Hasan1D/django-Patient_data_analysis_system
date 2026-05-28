from rest_framework import serializers

from core.models import SupportMessage, SupportTicket


class SupportMessageSerializer(serializers.ModelSerializer):
    user_name = serializers.CharField(source="user.username", read_only=True)
    user_role = serializers.CharField(source="user.role", read_only=True)

    class Meta:
        model = SupportMessage
        fields = ["id", "ticket", "user", "user_name", "user_role", "message", "created_at"]
        read_only_fields = ["ticket", "user", "created_at"]


class SupportTicketSerializer(serializers.ModelSerializer):
    messages = SupportMessageSerializer(many=True, read_only=True)
    user_name = serializers.CharField(source="user.username", read_only=True)

    class Meta:
        model = SupportTicket
        fields = ["id", "user", "user_name", "subject", "status", "priority", "created_at", "updated_at", "messages"]
        read_only_fields = ["user", "created_at", "updated_at"]
