from django.db import transaction
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.models import Doctor, Hospital, User
from core.permissions import IsAdminOnly
from core.serializers import (
    DoctorSerializer,
    HospitalSerializer,
    UserAccountCreateSerializer,
    UserSerializer,
)
from core.services.account_service import approve_user_account


class UserViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = User.objects.all().order_by("id")
    serializer_class = UserSerializer
    permission_classes = [IsAdminOnly]

    @action(detail=False, methods=["post"], url_path="create-account")
    def create_account(self, request):
        serializer = UserAccountCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None):
        user = self.get_object()
        if user.role != User.ROLE_DOCTOR:
            return Response(
                {"role": "Only doctor accounts require admin approval."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not Doctor.objects.filter(user=user).exists():
            return Response(
                {"doctor": "No Doctor record is linked to this user."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            approve_user_account(user)
            user.refresh_from_db()

        if user.is_active:
            message = "Doctor account approved and activated."
        else:
            message = "Doctor account approved. It will activate after email verification."

        return Response(
            {
                "message": message,
                "user": UserSerializer(user).data,
            }
        )


class DoctorViewSet(viewsets.ModelViewSet):
    queryset = Doctor.objects.all()
    serializer_class = DoctorSerializer
    permission_classes = [IsAuthenticated]


class HospitalViewSet(viewsets.ModelViewSet):
    queryset = Hospital.objects.all()
    serializer_class = HospitalSerializer
    permission_classes = [IsAuthenticated]
