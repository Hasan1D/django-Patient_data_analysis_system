from django.db import transaction
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import Hospital
from core.serializers import (
    EmailVerificationSerializer,
    RegistrationHospitalSerializer,
    ResendEmailVerificationSerializer,
    UserRegistrationSerializer,
    UserSerializer,
)
from core.services.email_verification import send_email_verification_code, verify_email_code


class RegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = UserRegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            user = serializer.save()
            send_email_verification_code(user)

        return Response(
            {
                "message": "Registration successful. Please check your email for the verification code.",
                "user": UserSerializer(user).data,
            },
            status=status.HTTP_201_CREATED,
        )


class RegistrationHospitalListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        hospitals = Hospital.objects.all().order_by("name", "id")
        serializer = RegistrationHospitalSerializer(hospitals, many=True)
        return Response(serializer.data)


class VerifyEmailView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = EmailVerificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            verify_email_code(serializer.user, serializer.validated_data["code"])
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        serializer.user.refresh_from_db()
        if serializer.user.is_active:
            message = "Account verified and activated successfully."
        else:
            message = "Email verified successfully. Account is pending admin approval."

        return Response(
            {
                "message": message,
                "user": UserSerializer(serializer.user).data,
            }
        )


class ResendEmailVerificationView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ResendEmailVerificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        send_email_verification_code(serializer.user)

        return Response({"message": "Verification code sent."})


from rest_framework_simplejwt.views import TokenObtainPairView
from ..serializers import CustomTokenObtainPairSerializer

class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer

