from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from core.models import Hospital, User
from core.services.account_service import create_user_account


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = (
            "id",
            "username",
            "real_name",
            "phon_number",
            "email",
            "role",
            "is_active",
            "email_verified",
            "admin_approved",
            "is_staff",
        )
        read_only_fields = ("id", "is_staff", "email_verified", "admin_approved")


class UserRegistrationSerializer(serializers.ModelSerializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    password_confirm = serializers.CharField(write_only=True, trim_whitespace=False)
    specialization = serializers.CharField(write_only=True, trim_whitespace=True)
    hospital_id = serializers.IntegerField(write_only=True)

    class Meta:
        model = User
        fields = (
            "id",
            "username",
            "real_name",
            "phon_number",
            "email",
            "password",
            "password_confirm",
            "specialization",
            "hospital_id",
        )
        read_only_fields = ("id",)

    def validate_email(self, value):
        normalized_value = value.strip().lower()
        if not normalized_value:
            raise serializers.ValidationError("Email is required.")
        if User.objects.filter(email__iexact=normalized_value).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return normalized_value

    def validate(self, attrs):
        if attrs["password"] != attrs["password_confirm"]:
            raise serializers.ValidationError({"password_confirm": "Passwords do not match."})
        validate_password(attrs["password"])

        errors = {}
        specialization = attrs.get("specialization", "")
        if not specialization.strip():
            errors["specialization"] = "specialization is required for doctor registration."

        hospital_id = attrs.get("hospital_id")
        try:
            attrs["hospital"] = Hospital.objects.get(id=hospital_id)
        except Hospital.DoesNotExist:
            errors["hospital_id"] = "Hospital with this id does not exist."

        if errors:
            raise serializers.ValidationError(errors)
        return attrs

    def create(self, validated_data):
        validated_data.pop("password_confirm")
        password = validated_data.pop("password")
        specialization = validated_data.pop("specialization")
        validated_data.pop("hospital_id")
        hospital = validated_data.pop("hospital")
        return create_user_account(
            user_data={
                **validated_data,
                "role": User.ROLE_DOCTOR,
                "is_active": False,
                "email_verified": False,
                "admin_approved": False,
            },
            password=password,
            specialization=specialization,
            hospital=hospital,
        )


class UserAccountCreateSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    password_confirm = serializers.CharField(write_only=True, trim_whitespace=False)
    specialization = serializers.CharField(required=False, allow_blank=True, write_only=True)
    hospital_id = serializers.IntegerField(required=False, write_only=True)

    class Meta:
        model = User
        fields = (
            "id",
            "username",
            "real_name",
            "phon_number",
            "email",
            "role",
            "password",
            "password_confirm",
            "is_active",
            "email_verified",
            "admin_approved",
            "is_staff",
            "specialization",
            "hospital_id",
        )
        read_only_fields = ("id",)
        extra_kwargs = {
            "is_active": {"required": False},
            "email_verified": {"required": False},
            "admin_approved": {"required": False},
            "is_staff": {"required": False},
        }

    def validate_email(self, value):
        normalized_value = value.strip().lower()
        if normalized_value and User.objects.filter(email__iexact=normalized_value).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return normalized_value

    def validate(self, attrs):
        if attrs["password"] != attrs["password_confirm"]:
            raise serializers.ValidationError({"password_confirm": "Passwords do not match."})
        validate_password(attrs["password"])

        role = attrs.get("role")
        specialization = attrs.get("specialization", "")
        hospital_id = attrs.get("hospital_id")

        if role == User.ROLE_DOCTOR:
            errors = {}
            if not specialization.strip():
                errors["specialization"] = "specialization is required for doctor users."
            if hospital_id is None:
                errors["hospital_id"] = "hospital_id is required for doctor users."
            else:
                try:
                    attrs["hospital"] = Hospital.objects.get(id=hospital_id)
                except Hospital.DoesNotExist:
                    errors["hospital_id"] = "Hospital with this id does not exist."

            if errors:
                raise serializers.ValidationError(errors)
        elif role == User.ROLE_ADMIN:
            attrs.pop("specialization", None)
            attrs.pop("hospital_id", None)
        else:
            raise serializers.ValidationError({"role": "role must be admin or doctor."})

        return attrs

    def create(self, validated_data):
        validated_data.pop("password_confirm")
        password = validated_data.pop("password")
        specialization = validated_data.pop("specialization", None)
        validated_data.pop("hospital_id", None)
        hospital = validated_data.pop("hospital", None)
        is_active = validated_data.get("is_active", True)
        if is_active:
            validated_data["email_verified"] = True
            validated_data["admin_approved"] = True
        else:
            validated_data["email_verified"] = False
            validated_data["admin_approved"] = False

        return create_user_account(
            user_data=validated_data,
            password=password,
            specialization=specialization,
            hospital=hospital,
        )


class EmailVerificationSerializer(serializers.Serializer):
    email = serializers.EmailField()
    code = serializers.RegexField(regex=r"^\d+$", max_length=12)

    def validate_email(self, value):
        return value.strip().lower()

    def validate(self, attrs):
        users = list(User.objects.filter(email__iexact=attrs["email"])[:2])
        if not users:
            raise serializers.ValidationError({"email": "No account was found for this email."})
        if len(users) > 1:
            raise serializers.ValidationError({"email": "More than one account uses this email."})
        self.user = users[0]
        return attrs


class ResendEmailVerificationSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def validate_email(self, value):
        normalized_value = value.strip().lower()
        users = list(User.objects.filter(email__iexact=normalized_value)[:2])
        if not users:
            raise serializers.ValidationError("No account was found for this email.")
        if len(users) > 1:
            raise serializers.ValidationError("More than one account uses this email.")
        user = users[0]
        if user.email_verified:
            raise serializers.ValidationError("This account email is already verified.")
        if user.is_active and not hasattr(user, "email_verification_code"):
            raise serializers.ValidationError("This account is already active.")
        self.user = user
        return normalized_value


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    def validate(self, attrs):
        username = attrs.get(self.username_field)
        password = attrs.get("password")

        if username and password:
            user = User.objects.filter(**{self.username_field: username}).first()
            if user:
                if user.check_password(password):
                    if not user.is_active:
                        raise AuthenticationFailed({"is_active": False})
                else:
                    raise AuthenticationFailed({"invalid_credentials": True})
            else:
                raise AuthenticationFailed({"invalid_credentials": True})
        else:
            raise AuthenticationFailed({"invalid_credentials": True})

        return super().validate(attrs)
