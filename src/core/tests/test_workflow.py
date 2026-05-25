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

class PatientWorkflowTests(CoreAPITestCase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.doctor_user)

    def _visit_payload(self, **overrides):
        payload = {
            "disease_code": self.disease_a.disease_code,
            "diagnosis_date": "2026-04-22",
            "status": "infected",
            "weight": 72,
            "height": 176,
            "marital_status": "single",
        }
        payload.update(overrides)
        return payload

    def test_patient_creation_automatically_creates_one_medical_history(self):
        response = self.client.post(
            reverse("patient-list"),
            {
                "national_number": "WF-1001",
                "name": "Workflow Patient",
                "birth_date": "1991-05-10",
                "gender": "female",
                "residence_lat": 33.6100,
                "residence_long": 36.4100,
                "work_lat": 33.6200,
                "work_long": 36.4200,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        patient = Patient.objects.get(id=response.json()["id"])
        MedicalHistory.objects.get_or_create(patient=patient)

        self.assertEqual(MedicalHistory.objects.filter(patient=patient).count(), 1)

    def test_patient_creation_allows_missing_work_coordinates(self):
        response = self.client.post(
            reverse("patient-list"),
            {
                "national_number": "WF-1003",
                "name": "No Work Coordinates Patient",
                "birth_date": "1988-07-12",
                "gender": "male",
                "residence_lat": 33.7100,
                "residence_long": 36.5100,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        patient = Patient.objects.get(id=response.json()["id"])
        self.assertEqual(patient.residence_lat, 33.7100)
        self.assertEqual(patient.residence_long, 36.5100)
        self.assertIsNone(patient.work_lat)
        self.assertIsNone(patient.work_long)

    def test_create_visit_from_patient_endpoint_links_patient_and_creates_geodata(self):
        response = self.client.post(
            reverse("patient-visits", args=[self.patient_one.id]),
            self._visit_payload(),
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        visit = Visit.objects.get(id=response.json()["id"])
        self.assertEqual(visit.patient_id, self.patient_one.id)

        home_geodata = GeoData.objects.get(visit=visit, region_type="home")
        work_geodata = GeoData.objects.get(visit=visit, region_type="work")
        self.assertEqual(home_geodata.patient_id, self.patient_one.id)
        self.assertEqual(visit.doctor_id, self.doctor.id)
        self.assertEqual(visit.disease_id, self.disease_a.id)
        self.assertEqual(response.json()["doctor_info"]["id"], self.doctor.id)
        self.assertEqual(response.json()["doctor_info"]["real_name"], "Doctor One")
        self.assertEqual(response.json()["doctor_info"]["specialization"], "Epidemiology")
        self.assertEqual(home_geodata.latitude, self.patient_one.residence_lat)
        self.assertEqual(home_geodata.longitude, self.patient_one.residence_long)
        self.assertEqual(work_geodata.latitude, self.patient_one.work_lat)
        self.assertEqual(work_geodata.longitude, self.patient_one.work_long)

    def test_patient_visits_endpoint_lists_doctor_info_for_visit_history(self):
        response = self.client.get(reverse("patient-visits", args=[self.patient_one.id]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        visits_by_id = {visit["id"]: visit for visit in response.json()}

        self.assertIn(self.visit_a1.id, visits_by_id)
        self.assertIn(self.visit_b1.id, visits_by_id)
        previous_doctor_info = visits_by_id[self.visit_b1.id]["doctor_info"]
        self.assertEqual(previous_doctor_info["id"], self.second_doctor.id)
        self.assertEqual(previous_doctor_info["username"], "doctor2")
        self.assertEqual(previous_doctor_info["real_name"], "Doctor Two")
        self.assertEqual(previous_doctor_info["specialization"], "Internal Medicine")
        self.assertEqual(previous_doctor_info["hospital"], self.hospital.id)
        self.assertEqual(previous_doctor_info["hospital_name"], "Central Hospital")

    def test_doctor_creates_visit_with_disease_code_without_doctor_id(self):
        response = self.client.post(
            reverse("patient-visits", args=[self.patient_one.id]),
            self._visit_payload(disease_code=" mea "),
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        visit = Visit.objects.get(id=response.json()["id"])
        self.assertEqual(visit.doctor_id, self.doctor.id)
        self.assertEqual(visit.disease_id, self.disease_a.id)

    def test_create_visit_from_patient_endpoint_rejects_unknown_disease_code(self):
        response = self.client.post(
            reverse("patient-visits", args=[self.patient_one.id]),
            self._visit_payload(disease_code="missing-code"),
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("disease_code", response.json())

    def test_doctor_user_without_doctor_record_cannot_create_patient_visit(self):
        doctor_without_record = User.objects.create_user(
            username="doctor-without-record",
            password="secret123",
            real_name="Doctor Without Record",
            phon_number="0098",
            role="doctor",
        )
        self.client.force_authenticate(user=doctor_without_record)

        response = self.client.post(
            reverse("patient-visits", args=[self.patient_one.id]),
            self._visit_payload(),
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("doctor", response.json())

    def test_admin_creates_patient_visit_with_explicit_doctor_id(self):
        self.client.force_authenticate(user=self.admin_user)

        response = self.client.post(
            reverse("patient-visits", args=[self.patient_one.id]),
            self._visit_payload(doctor_id=self.second_doctor.id),
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        visit = Visit.objects.get(id=response.json()["id"])
        self.assertEqual(visit.doctor_id, self.second_doctor.id)
        self.assertEqual(visit.disease_id, self.disease_a.id)

    def test_admin_patient_visit_requires_doctor_id(self):
        self.client.force_authenticate(user=self.admin_user)

        response = self.client.post(
            reverse("patient-visits", args=[self.patient_one.id]),
            self._visit_payload(),
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("doctor_id", response.json())

    def test_create_visit_from_patient_endpoint_ignores_missing_work_coordinates(self):
        patient = Patient.objects.create(
            national_number="WF-1002",
            name="Home Only Patient",
            birth_date=date(1985, 3, 8),
            gender="male",
            residence_lat=33.7000,
            residence_long=36.5000,
            work_lat=None,
            work_long=None,
        )

        response = self.client.post(
            reverse("patient-visits", args=[patient.id]),
            self._visit_payload(),
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        visit = Visit.objects.get(id=response.json()["id"])
        geodata = GeoData.objects.filter(visit=visit).order_by("region_type")
        self.assertEqual(list(geodata.values_list("region_type", flat=True)), ["home"])
        home_geodata = geodata.get(region_type="home")
        self.assertEqual(home_geodata.latitude, patient.residence_lat)
        self.assertEqual(home_geodata.longitude, patient.residence_long)
        self.assertFalse(GeoData.objects.filter(visit=visit, region_type="work").exists())

    def test_auto_geodata_creation_does_not_duplicate_visit_region_type(self):
        response = self.client.post(
            reverse("patient-visits", args=[self.patient_one.id]),
            self._visit_payload(),
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        visit = Visit.objects.get(id=response.json()["id"])

        create_geodata_from_patient_coordinates(visit=visit)
        create_geodata_from_patient_coordinates(visit=visit)

        self.assertEqual(GeoData.objects.filter(visit=visit, region_type="home").count(), 1)
        self.assertEqual(GeoData.objects.filter(visit=visit, region_type="work").count(), 1)

    def test_create_visit_from_patient_endpoint_rejects_patient_in_body(self):
        response = self.client.post(
            reverse("patient-visits", args=[self.patient_one.id]),
            self._visit_payload(patient=self.patient_two.id),
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("patient", response.json())

    def test_add_lab_test_through_visit_workflow(self):
        response = self.client.post(
            reverse("visit-lab-tests", args=[self.visit_a1.id]),
            {
                "test_code": "CBC",
                "test_name": "Complete Blood Count",
                "result": "Normal",
                "test_date": "2026-04-22T10:30:00Z",
                "notes": "Workflow lab test",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        lab_test = LabTest.objects.get(id=response.json()["id"])
        self.assertEqual(lab_test.visit_id, self.visit_a1.id)
        self.assertEqual(response.json()["visit"], self.visit_a1.id)

    def test_add_medical_history_details_through_patient_workflow(self):
        history = MedicalHistory.objects.get(patient=self.patient_one)

        allergy_response = self.client.post(
            reverse("patient-allergies", args=[self.patient_one.id]),
            {"allergy_name": "Penicillin", "severity_level": "high"},
        )
        chronic_response = self.client.post(
            reverse("patient-chronic-diseases", args=[self.patient_one.id]),
            {"disease_name": "Asthma", "diagnosis_date": "2020-01-15"},
        )
        vaccine_response = self.client.post(
            reverse("patient-vaccines", args=[self.patient_one.id]),
            {"vaccine_name": "Influenza", "date_administered": "2025-10-01"},
        )
        surgery_response = self.client.post(
            reverse("patient-surgeries", args=[self.patient_one.id]),
            {
                "surgery_description": "Appendectomy",
                "surgery_date": "2019-06-20",
                "has_metal_plates": False,
            },
        )

        self.assertEqual(allergy_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(chronic_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(vaccine_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(surgery_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            Allergy.objects.get(id=allergy_response.json()["id"]).history_id,
            history.id,
        )
        self.assertEqual(
            chronicDisease.objects.get(id=chronic_response.json()["id"]).history_id,
            history.id,
        )
        self.assertEqual(
            Vaccine.objects.get(id=vaccine_response.json()["id"]).history_id,
            history.id,
        )
        self.assertEqual(
            SurgicalHistory.objects.get(id=surgery_response.json()["id"]).history_id,
            history.id,
        )

class SerializerTests(CoreAPITestCase):
    def test_user_serializer_excludes_password_field(self):
        serializer = UserSerializer(self.doctor_user)

        self.assertNotIn("password", serializer.data)
        self.assertEqual(serializer.data["username"], self.doctor_user.username)

    def test_user_serializer_rejects_role_outside_admin_or_doctor(self):
        serializer = UserSerializer(
            data={
                "username": "visitor",
                "real_name": "Visitor User",
                "phon_number": "0004",
                "role": "guest",
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("role", serializer.errors)

    def test_patient_serializer_rejects_gender_outside_male_or_female(self):
        serializer = PatientSerializer(
            data={
                "national_number": "9000",
                "name": "Invalid Gender Patient",
                "birth_date": "1990-01-01",
                "gender": "other",
                "residence_lat": 33.50,
                "residence_long": 36.25,
                "work_lat": 33.52,
                "work_long": 36.26,
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("gender", serializer.errors)

    def test_visit_serializer_rejects_status_outside_infected_or_cured(self):
        serializer = VisitSerializer(
            data={
                "patient": self.patient_one.id,
                "doctor": self.doctor.id,
                "disease": self.disease_a.id,
                "diagnosis_date": "2026-04-12",
                "status": "confirmed",
                "weight": 70,
                "height": 175,
                "marital_status": "single",
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("status", serializer.errors)

    def test_visit_serializer_rejects_marital_status_outside_allowed_choices(self):
        serializer = VisitSerializer(
            data={
                "patient": self.patient_one.id,
                "doctor": self.doctor.id,
                "disease": self.disease_a.id,
                "diagnosis_date": "2026-04-12",
                "status": "infected",
                "weight": 70,
                "height": 175,
                "marital_status": "engaged",
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("marital_status", serializer.errors)

    def test_disease_serializer_normalizes_code_and_rejects_negative_scores(self):
        serializer = DiseaseSerializer(
            data={
                "disease_code": " mea ",
                "name": " Measles ",
                "type": "viral",
                "transmission_vector": "airborne",
                "symptoms": "fever",
                "risk_level": 4,
                "infection_score": 0.92,
            }
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["disease_code"], "MEA")
        self.assertEqual(serializer.validated_data["name"], "Measles")

        invalid_serializer = DiseaseSerializer(
            data={
                "disease_code": "col",
                "name": "Cholera",
                "type": "bacterial",
                "transmission_vector": "waterborne",
                "symptoms": "diarrhea",
                "risk_level": 4,
                "infection_score": -0.5,
            }
        )
        self.assertFalse(invalid_serializer.is_valid())
        self.assertIn("infection_score", invalid_serializer.errors)

    def test_report_serializer_validates_dates_and_trigger_visit_disease(self):
        report = Report(
            disease=self.disease_a,
            trigger_visit=self.visit_a1,
            analysis_period_start=date(2026, 4, 1),
            analysis_period_end=date(2026, 4, 2),
            alert_level="medium",
            summary="Test report",
            risk_score=10.0,
            nearby_case_count=0,
            current_case_count=1,
            previous_case_count=0,
            growth_rate=0.0,
            surge_ratio=1.0,
            reasons=["ok"],
            status="new",
        )

        invalid_dates_serializer = ReportSerializer(
            instance=report,
            data={"analysis_period_start": "2026-04-03", "analysis_period_end": "2026-04-01"},
            partial=True,
        )
        self.assertFalse(invalid_dates_serializer.is_valid())
        self.assertIn("analysis_period_end", invalid_dates_serializer.errors)

        invalid_visit_serializer = ReportSerializer(
            data={
                "disease": self.disease_a.id,
                "trigger_visit": self.visit_b1.id,
                "analysis_period_start": "2026-04-01",
                "analysis_period_end": "2026-04-01",
                "alert_level": "medium",
                "summary": "Cross disease report",
                "risk_score": 15.0,
                "nearby_case_count": 0,
                "current_case_count": 1,
                "previous_case_count": 0,
                "growth_rate": 0.0,
                "surge_ratio": 1.0,
                "reasons": ["invalid"],
                "status": "new",
            },
        )
        self.assertFalse(invalid_visit_serializer.is_valid())
        self.assertIn("trigger_visit", invalid_visit_serializer.errors)

    def test_geodata_serializer_rejects_patient_that_does_not_match_visit_patient(self):
        serializer = GeoDataSerializer(
            data={
                "patient": self.patient_two.id,
                "visit": self.visit_a1.id,
                "latitude": 33.5000,
                "longitude": 36.2500,
                "region_type": "home",
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("patient", serializer.errors)

    def test_geodata_serializer_rejects_region_type_outside_home_or_work(self):
        serializer = GeoDataSerializer(
            data={
                "patient": self.patient_one.id,
                "visit": self.visit_a1.id,
                "latitude": 33.5000,
                "longitude": 36.2500,
                "region_type": "school",
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("region_type", serializer.errors)

    def test_medical_history_serializer_allows_only_one_history_per_patient(self):
        serializer = MedicalHistorySerializer(
            data={
                "patient": self.patient_one.id,
                "has_surgical_metal_plates": True,
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("patient", serializer.errors)
