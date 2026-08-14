from datetime import date
from pathlib import Path
import csv
import pickle
import re
import shutil
import time
from tempfile import NamedTemporaryFile
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from core.admin import UserAccountAdminCreationForm, UserAdmin as CoreUserAdmin
from core.services.account_service import create_linked_doctor_for_user
from core.services import DiseaseAlertPolicy, OutbreakAnalysis, TrendSnapshot, VisitOutbreakContext
from core.services.active_cases import active_visits_queryset
from core.services.alert_policies import get_policy_for_disease
from core.services.anomaly_detection import detect_temporal_anomalies
from core.services.cluster_service import ALERT_LEVEL_TO_RISK_LEVEL, create_cluster_from_analysis
from core.services.dbscan_hotspots import detect_dbscan_clusters, persist_dbscan_clusters
from core.services.disease_reference import REFERENCE_SOURCE_NAME, classify_reference_disease
from core.services.hdbscan_hotspots import detect_hdbscan_clusters, persist_hdbscan_clusters
from core.services.ml import random_forest as random_forest_service
from core.services.ml.random_forest import predict_alert_level, train_random_forest_model
from core.services.outbreak_engine import evaluate_visit_outbreak
from core.services.report_service import create_report_from_analysis
from core.services.risk_prediction import (
    build_training_samples,
    load_risk_model,
    predict_visit_risk,
    train_baseline_risk_model,
)
from core.services.spatial import distance_km, find_nearby_cases
from core.services.trend import build_trend_snapshot, count_cases_for_window
from core.services.workflow_service import create_geodata_from_patient_coordinates
from core.serializers import (
    DiseaseSerializer,
    GeoDataSerializer,
    MedicalHistorySerializer,
    PatientSerializer,
    ReportSerializer,
    UserSerializer,
    VisitSerializer,
)
from core.models import (
    Allergy,
    AuditLog,
    Disease,
    Doctor,
    EmailVerificationCode,
    GeoCluster,
    GeoData,
    Hospital,
    LabTest,
    MedicalHistory,
    Patient,
    Report,
    SurgicalHistory,
    Vaccine,
    Visit,
    chronicDisease,
)


User = get_user_model()


from .test_base import *

class PermissionTests(CoreAPITestCase):
    def test_users_endpoint_is_admin_only(self):
        self.client.force_authenticate(user=self.doctor_user)

        response = self.client.get(reverse("user-list"))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_can_list_users_endpoint(self):
        self.client.force_authenticate(user=self.admin_user)

        response = self.client.get(reverse("user-list"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        usernames = [item["username"] for item in response.json()]
        self.assertIn(self.admin_user.username, usernames)
        self.assertIn(self.doctor_user.username, usernames)
        self.assertIn(self.second_doctor_user.username, usernames)
        self.assertNotIn("password", response.json()[0])

    def test_doctors_endpoint_requires_authentication(self):
        response = self.client.get(reverse("doctor-list"))

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_doctor_cannot_create_doctor_hospital_or_disease_records(self):
        self.client.force_authenticate(user=self.doctor_user)

        doctor_response = self.client.post(
            reverse("doctor-list"),
            {
                "user": self.second_doctor_user.id,
                "specialization": "Neurology",
                "hospital": self.hospital.id,
            },
        )
        hospital_response = self.client.post(
            reverse("hospital-list"),
            {
                "name": "Private Hospital",
                "hospital_lat": 33.5,
                "hospital_long": 36.3,
                "location": "Center",
                "city": "Damascus",
            },
        )
        disease_response = self.client.post(
            reverse("disease-list"),
            {
                "disease_code": "NEW",
                "name": "New Disease",
                "type": "viral",
                "transmission_vector": "airborne",
                "symptoms": "fever",
                "risk_level": 2,
                "infection_score": 0.5,
            },
        )

        self.assertEqual(doctor_response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(hospital_response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(disease_response.status_code, status.HTTP_403_FORBIDDEN)

    def test_doctor_lists_are_limited_to_own_hospital_scope(self):
        other_hospital = Hospital.objects.create(
            name="Other Hospital",
            hospital_lat=34.51,
            hospital_long=37.29,
            location="Other",
            city="Homs",
        )
        other_user = User.objects.create_user(
            username="other-doctor",
            password="secret123",
            real_name="Other Doctor",
            phon_number="0009",
            role=User.ROLE_DOCTOR,
            email_verified=True,
            admin_approved=True,
        )
        other_doctor = Doctor.objects.create(
            user=other_user,
            specialization="Surgery",
            hospital=other_hospital,
        )
        self.client.force_authenticate(user=self.doctor_user)

        doctors_response = self.client.get(reverse("doctor-list"))
        hospitals_response = self.client.get(reverse("hospital-list"))

        self.assertEqual(doctors_response.status_code, status.HTTP_200_OK)
        self.assertEqual(hospitals_response.status_code, status.HTTP_200_OK)
        self.assertNotIn(other_doctor.id, [doctor["id"] for doctor in doctors_response.json()])
        self.assertEqual(
            [hospital["id"] for hospital in hospitals_response.json()],
            [self.hospital.id],
        )

    def test_doctor_cannot_retrieve_visit_from_another_hospital(self):
        other_hospital = Hospital.objects.create(
            name="Outside Hospital",
            hospital_lat=34.51,
            hospital_long=37.29,
            location="Outside",
            city="Homs",
        )
        other_user = User.objects.create_user(
            username="outside-doctor",
            password="secret123",
            real_name="Outside Doctor",
            phon_number="0010",
            role=User.ROLE_DOCTOR,
            email_verified=True,
            admin_approved=True,
        )
        other_doctor = Doctor.objects.create(
            user=other_user,
            specialization="Surgery",
            hospital=other_hospital,
        )
        outside_visit = Visit.objects.create(
            patient=self.patient_one,
            doctor=other_doctor,
            disease=self.disease_a,
            diagnosis_date=date(2026, 4, 25),
            status="infected",
            weight=70,
            height=175,
            marital_status="single",
        )
        self.client.force_authenticate(user=self.doctor_user)

        response = self.client.get(reverse("visit-detail", args=[outside_visit.id]))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_api_requests_are_audited(self):
        self.client.force_authenticate(user=self.doctor_user)

        response = self.client.get(reverse("doctor-me"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        audit_log = AuditLog.objects.latest("id")
        self.assertEqual(audit_log.user_id, self.doctor_user.id)
        self.assertEqual(audit_log.method, "GET")
        self.assertEqual(audit_log.path, reverse("doctor-me"))
        self.assertEqual(audit_log.status_code, status.HTTP_200_OK)

    def test_doctor_can_view_own_profile(self):
        self.client.force_authenticate(user=self.doctor_user)

        response = self.client.get(reverse("doctor-me"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["id"], self.doctor.id)
        self.assertEqual(response.json()["user"], self.doctor_user.id)
        self.assertEqual(response.json()["hospital"], self.hospital.id)

    def test_doctor_can_update_own_profile_without_changing_user_link(self):
        self.client.force_authenticate(user=self.doctor_user)

        response = self.client.patch(
            reverse("doctor-me"),
            {
                "specialization": "Cardiology",
                "user": self.second_doctor_user.id,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.doctor.refresh_from_db()
        self.assertEqual(self.doctor.specialization, "Cardiology")
        self.assertEqual(self.doctor.user_id, self.doctor_user.id)
        self.assertEqual(response.json()["user"], self.doctor_user.id)

    def test_admin_cannot_use_doctor_me_endpoint(self):
        self.client.force_authenticate(user=self.admin_user)

        response = self.client.get(reverse("doctor-me"))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_visits_endpoint_requires_doctor_or_admin(self):
        response = self.client.get(reverse("visit-list"))

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_report_generate_is_admin_only(self):
        self.client.force_authenticate(user=self.doctor_user)

        response = self.client.get(
            reverse("report-generate"),
            {"disease": self.disease_a.id, "region_type": "home"},
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_can_preview_report_with_get_request_without_persisting(self):
        self.client.force_authenticate(user=self.admin_user)

        response = self.client.get(
            reverse("report-generate"),
            {"disease": self.disease_a.id, "region_type": "home"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Report.objects.count(), 0)
        self.assertIsNone(response.json()["id"])
        self.assertEqual(response.json()["disease"], self.disease_a.id)
        self.assertEqual(response.json()["analysis_period_start"], "2026-04-01")
        self.assertEqual(response.json()["analysis_period_end"], "2026-04-01")
        self.assertEqual(response.json()["current_case_count"], 1)
        self.assertEqual(response.json()["alert_level"], "medium")

    def test_admin_can_generate_report_with_post_request(self):
        self.client.force_authenticate(user=self.admin_user)

        response = self.client.post(
            reverse("report-generate"),
            {"disease": self.disease_a.id, "region_type": "home"},
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Report.objects.count(), 1)
        self.assertIsNotNone(response.json()["id"])
        self.assertEqual(response.json()["disease"], self.disease_a.id)

    def test_doctor_can_evaluate_outbreak_for_visit(self):
        self.client.force_authenticate(user=self.doctor_user)

        response = self.client.get(reverse("visit-evaluate-outbreak", args=[self.visit_a1.id]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["visit_id"], self.visit_a1.id)
        self.assertEqual(response.json()["disease"]["id"], self.disease_a.id)
        self.assertIn("analysis", response.json())
        self.assertIn("policy", response.json())

class AccountCreationTests(CoreAPITestCase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.admin_user)

    def _account_payload(self, **overrides):
        payload = {
            "username": "api-doctor",
            "real_name": "API Doctor",
            "phon_number": "0900",
            "email": "api-doctor@example.com",
            "role": "doctor",
            "password": "ComplexPass123!",
            "password_confirm": "ComplexPass123!",
            "specialization": "Internal Medicine",
            "hospital_id": self.hospital.id,
            "is_active": True,
            "is_staff": False,
        }
        payload.update(overrides)
        return payload

    def test_doctor_user_created_through_api_creates_linked_doctor(self):
        response = self.client.post(reverse("user-create-account"), self._account_payload())

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        user = User.objects.get(username="api-doctor")
        doctor = Doctor.objects.get(user=user)
        self.assertEqual(user.role, User.ROLE_DOCTOR)
        self.assertTrue(user.email_verified)
        self.assertTrue(user.admin_approved)
        self.assertEqual(doctor.specialization, "Internal Medicine")
        self.assertEqual(doctor.hospital_id, self.hospital.id)

    def test_admin_user_created_through_api_does_not_create_doctor(self):
        response = self.client.post(
            reverse("user-create-account"),
            self._account_payload(
                username="api-admin",
                email="api-admin@example.com",
                role="admin",
                specialization="Ignored",
                hospital_id=self.hospital.id,
                is_staff=True,
            ),
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        user = User.objects.get(username="api-admin")
        self.assertEqual(user.role, User.ROLE_ADMIN)
        self.assertTrue(user.email_verified)
        self.assertTrue(user.admin_approved)
        self.assertFalse(Doctor.objects.filter(user=user).exists())

    def test_doctor_account_creation_requires_specialization(self):
        response = self.client.post(
            reverse("user-create-account"),
            self._account_payload(specialization=""),
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("specialization", response.json())

    def test_doctor_account_creation_requires_hospital_id(self):
        payload = self._account_payload()
        payload.pop("hospital_id")

        response = self.client.post(reverse("user-create-account"), payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("hospital_id", response.json())

    def test_doctor_account_creation_rejects_invalid_hospital_id(self):
        response = self.client.post(
            reverse("user-create-account"),
            self._account_payload(hospital_id=999999),
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("hospital_id", response.json())

    def test_admin_side_doctor_creation_creates_linked_doctor(self):
        form = UserAccountAdminCreationForm(
            data={
                "username": "admin-side-doctor",
                "real_name": "Admin Side Doctor",
                "phon_number": "0901",
                "email": "admin-side-doctor@example.com",
                "role": "doctor",
                "password1": "ComplexPass123!",
                "password2": "ComplexPass123!",
                "specialization": "Cardiology",
                "hospital": self.hospital.id,
                "is_staff": False,
                "is_active": True,
            }
        )
        self.assertTrue(form.is_valid(), form.errors)

        admin_view = CoreUserAdmin(User, AdminSite())
        user = form.save(commit=False)
        admin_view.save_model(
            SimpleNamespace(user=self.admin_user),
            user,
            form,
            change=False,
        )

        doctor = Doctor.objects.get(user=user)
        user.refresh_from_db()
        self.assertEqual(doctor.specialization, "Cardiology")
        self.assertEqual(doctor.hospital_id, self.hospital.id)
        self.assertTrue(user.email_verified)
        self.assertTrue(user.admin_approved)

    def test_duplicate_doctor_profile_creation_is_prevented(self):
        with self.assertRaisesMessage(ValueError, "Doctor record already exists for this user."):
            create_linked_doctor_for_user(
                user=self.doctor_user,
                specialization="Duplicate",
                hospital=self.hospital,
            )

@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    EMAIL_VERIFICATION_CODE_EXPIRY_MINUTES=10,
    EMAIL_VERIFICATION_CODE_LENGTH=6,
)
class EmailVerificationTests(APITestCase):
    def setUp(self):
        self.hospital = Hospital.objects.create(
            name="Registration Hospital",
            hospital_lat=33.51,
            hospital_long=36.29,
            location="Center",
            city="Damascus",
        )
        self.admin_user = User.objects.create_user(
            username="approval-admin",
            password="secret123",
            real_name="Approval Admin",
            phon_number="0001",
            email="approval-admin@example.com",
            role=User.ROLE_ADMIN,
            email_verified=True,
            admin_approved=True,
        )

    def _registration_payload(self, **overrides):
        payload = {
            "username": "newdoctor",
            "real_name": "New Doctor",
            "phon_number": "0099",
            "email": "newdoctor@example.com",
            "password": "ComplexPass123!",
            "password_confirm": "ComplexPass123!",
            "specialization": "Internal Medicine",
            "hospital_id": self.hospital.id,
        }
        payload.update(overrides)
        return payload

    def _register_user(self):
        response = self.client.post(
            reverse("auth-register"),
            self._registration_payload(),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        return User.objects.get(username="newdoctor")

    def _email_code(self):
        match = re.search(r"\b\d{6}\b", mail.outbox[-1].body)
        self.assertIsNotNone(match)
        return match.group(0)

    def test_registration_hospitals_are_available_without_authentication(self):
        response = self.client.get(reverse("auth-registration-hospitals"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.json(),
            [
                {
                    "id": self.hospital.id,
                    "name": "Registration Hospital",
                    "city": "Damascus",
                    "location": "Center",
                }
            ],
        )

    def test_registration_creates_inactive_user_and_sends_verification_code(self):
        response = self.client.post(
            reverse("auth-register"),
            self._registration_payload(),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        user = User.objects.get(username="newdoctor")
        self.assertFalse(user.is_active)
        self.assertFalse(user.email_verified)
        self.assertFalse(user.admin_approved)
        self.assertEqual(user.role, User.ROLE_DOCTOR)
        self.assertTrue(user.check_password("ComplexPass123!"))
        doctor = Doctor.objects.get(user=user)
        self.assertEqual(doctor.specialization, "Internal Medicine")
        self.assertEqual(doctor.hospital_id, self.hospital.id)
        self.assertTrue(EmailVerificationCode.objects.filter(user=user).exists())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("newdoctor@example.com", mail.outbox[0].to)
        self.assertRegex(mail.outbox[0].body, r"\b\d{6}\b")

    def test_registration_requires_specialization(self):
        response = self.client.post(
            reverse("auth-register"),
            self._registration_payload(specialization=""),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("specialization", response.json())

    def test_registration_requires_valid_hospital_id(self):
        response = self.client.post(
            reverse("auth-register"),
            self._registration_payload(hospital_id=999999),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("hospital_id", response.json())

    def test_registration_requires_hospital_id(self):
        payload = self._registration_payload()
        payload.pop("hospital_id")

        response = self.client.post(
            reverse("auth-register"),
            payload,
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("hospital_id", response.json())

    def test_correct_verification_code_marks_email_verified_but_waits_for_admin(self):
        user = self._register_user()
        code = self._email_code()

        response = self.client.post(
            reverse("auth-verify-email"),
            {"email": "newdoctor@example.com", "code": code},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertTrue(user.email_verified)
        self.assertFalse(user.admin_approved)
        self.assertFalse(user.is_active)
        self.assertFalse(EmailVerificationCode.objects.filter(user=user).exists())

    def test_admin_approval_after_email_verification_activates_doctor(self):
        user = self._register_user()
        code = self._email_code()
        self.client.post(
            reverse("auth-verify-email"),
            {"email": "newdoctor@example.com", "code": code},
            format="json",
        )

        self.client.force_authenticate(user=self.admin_user)
        response = self.client.post(reverse("user-approve", args=[user.id]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertTrue(user.email_verified)
        self.assertTrue(user.admin_approved)
        self.assertTrue(user.is_active)

    def test_admin_approval_before_email_verification_does_not_activate_doctor(self):
        user = self._register_user()

        self.client.force_authenticate(user=self.admin_user)
        response = self.client.post(reverse("user-approve", args=[user.id]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertFalse(user.email_verified)
        self.assertTrue(user.admin_approved)
        self.assertFalse(user.is_active)

    def test_admin_approval_requires_linked_doctor_record(self):
        user = User.objects.create_user(
            username="doctor-without-profile",
            password="secret123",
            real_name="Doctor Without Profile",
            phon_number="0098",
            email="doctor-without-profile@example.com",
            role=User.ROLE_DOCTOR,
            email_verified=True,
        )

        self.client.force_authenticate(user=self.admin_user)
        response = self.client.post(reverse("user-approve", args=[user.id]))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("doctor", response.json())

    def test_wrong_verification_code_keeps_user_inactive(self):
        user = self._register_user()
        code = self._email_code()
        wrong_code = "000000" if code != "000000" else "111111"

        response = self.client.post(
            reverse("auth-verify-email"),
            {"email": "newdoctor@example.com", "code": wrong_code},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        user.refresh_from_db()
        verification = EmailVerificationCode.objects.get(user=user)
        self.assertFalse(user.is_active)
        self.assertEqual(verification.attempts, 1)

    def test_resend_verification_replaces_code_and_sends_email(self):
        user = self._register_user()
        original_code = self._email_code()

        response = self.client.post(
            reverse("auth-resend-verification"),
            {"email": "newdoctor@example.com"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 2)
        response = self.client.post(
            reverse("auth-verify-email"),
            {"email": "newdoctor@example.com", "code": original_code},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        user.refresh_from_db()
        self.assertFalse(user.is_active)
