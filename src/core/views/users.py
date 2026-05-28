from django.db import transaction
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.models import Doctor, Hospital, User
from core.permissions import IsAdminOnly, IsAdminOrAuthenticatedReadOnly
from core.serializers import (
    DoctorMeSerializer,
    DoctorSerializer,
    HospitalSerializer,
    UserAccountCreateSerializer,
    UserSerializer,
)
from core.services.access_control import doctors_visible_to_user, hospitals_visible_to_user
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
    queryset = Doctor.objects.select_related("user", "hospital").all()
    serializer_class = DoctorSerializer

    def get_permissions(self):
        if self.action == "me":
            return [IsAuthenticated()]
        return [IsAdminOrAuthenticatedReadOnly()]

    def get_queryset(self):
        return doctors_visible_to_user(
            super().get_queryset(),
            self.request.user,
        ).order_by("id")

    @action(detail=False, methods=["get", "patch"], url_path="me")
    def me(self, request):
        if request.user.role != User.ROLE_DOCTOR:
            return Response(
                {"detail": "Only doctors can access this endpoint."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            doctor = Doctor.objects.get(user=request.user)
        except Doctor.DoesNotExist:
            return Response(
                {"detail": "No Doctor record is linked to the logged-in user."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if request.method == "PATCH":
            serializer = DoctorMeSerializer(doctor, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(serializer.data)

        serializer = DoctorMeSerializer(doctor)
        return Response(serializer.data)


class HospitalViewSet(viewsets.ModelViewSet):
    queryset = Hospital.objects.all()
    serializer_class = HospitalSerializer
    permission_classes = [IsAdminOrAuthenticatedReadOnly]

    def get_queryset(self):
        return hospitals_visible_to_user(
            super().get_queryset(),
            self.request.user,
        ).order_by("name", "id")
