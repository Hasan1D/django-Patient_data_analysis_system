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
from core.services.spatial_points import collect_active_spatial_points
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


def _unlink_with_retries(path: Path, retries: int = 5, delay_seconds: float = 0.2) -> None:
    for attempt in range(retries):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if attempt == retries - 1:
                raise
            time.sleep(delay_seconds)


class _FakeNumpy:
    @staticmethod
    def radians(values):
        return values

    @staticmethod
    def array(values, dtype=None):
        return values


class _FakeHDBSCANClusterer:
    def __init__(self, labels, probabilities):
        self._labels = labels
        self._probabilities = probabilities
        self.probabilities_ = []

    def fit_predict(self, coordinates):
        point_count = len(coordinates)
        self.probabilities_ = self._probabilities[:point_count]
        return self._labels[:point_count]


class _FakeHDBSCANModule:
    def __init__(self, labels, probabilities):
        self._labels = labels
        self._probabilities = probabilities

    def HDBSCAN(self, **kwargs):
        return _FakeHDBSCANClusterer(self._labels, self._probabilities)


class _FakeJoblib:
    @staticmethod
    def dump(payload, path):
        with open(path, "wb") as handle:
            pickle.dump(payload, handle)

    @staticmethod
    def load(path):
        with open(path, "rb") as handle:
            return pickle.load(handle)


class _FakeRandomForestClassifier:
    def __init__(self, **kwargs):
        self.classes_ = []

    def fit(self, feature_matrix, labels):
        label_set = set(labels)
        self.classes_ = [
            label
            for label in random_forest_service.LABEL_ORDER
            if label in label_set
        ]
        return self

    def predict(self, feature_matrix):
        return [self.classes_[-1]]

    def predict_proba(self, feature_matrix):
        if len(self.classes_) == 1:
            return [[1.0]]
        tail_probability = round(0.15 / (len(self.classes_) - 1), 4)
        probabilities = [tail_probability for _ in self.classes_]
        probabilities[-1] = 0.85
        return [probabilities]


def _fake_ml_dependencies():
    return _FakeNumpy, _FakeJoblib, _FakeRandomForestClassifier, "test-sklearn"


class CoreAPITestCase(APITestCase):
    def setUp(self):
        self.admin_user = User.objects.create_user(
            username="admin",
            password="secret123",
            real_name="Admin User",
            phon_number="0001",
            role="admin",
            email_verified=True,
            admin_approved=True,
        )
        self.doctor_user = User.objects.create_user(
            username="doctor1",
            password="secret123",
            real_name="Doctor One",
            phon_number="0002",
            role="doctor",
            email_verified=True,
            admin_approved=True,
        )
        self.second_doctor_user = User.objects.create_user(
            username="doctor2",
            password="secret123",
            real_name="Doctor Two",
            phon_number="0003",
            role="doctor",
            email_verified=True,
            admin_approved=True,
        )

        self.hospital = Hospital.objects.create(
            name="Central Hospital",
            hospital_lat=33.51,
            hospital_long=36.29,
            location="Center",
            city="Damascus",
        )
        self.doctor = Doctor.objects.create(
            user=self.doctor_user,
            specialization="Epidemiology",
            hospital=self.hospital,
        )
        self.second_doctor = Doctor.objects.create(
            user=self.second_doctor_user,
            specialization="Internal Medicine",
            hospital=self.hospital,
        )

        self.patient_one = Patient.objects.create(
            national_number="1001",
            name="Patient One",
            birth_date=date(1990, 1, 1),
            gender="male",
            residence_lat=33.50,
            residence_long=36.25,
            work_lat=33.52,
            work_long=36.26,
        )
        self.patient_two = Patient.objects.create(
            national_number="1002",
            name="Patient Two",
            birth_date=date(1992, 2, 2),
            gender="female",
            residence_lat=33.53,
            residence_long=36.27,
            work_lat=33.54,
            work_long=36.28,
        )

        self.disease_a = Disease.objects.create(
            disease_code="MEA",
            name="Measles",
            type="viral",
            transmission_vector="airborne",
            symptoms="fever",
            risk_level=5,
            infection_score=4.5,
        )
        self.disease_b = Disease.objects.create(
            disease_code="COL",
            name="Cholera",
            type="bacterial",
            transmission_vector="water",
            symptoms="diarrhea",
            risk_level=4,
            infection_score=3.0,
        )

        self.visit_a1 = Visit.objects.create(
            patient=self.patient_one,
            doctor=self.doctor,
            disease=self.disease_a,
            diagnosis_date=date(2026, 4, 1),
            status="infected",
            weight=70,
            height=175,
            marital_status="single",
        )
        self.visit_a2 = Visit.objects.create(
            patient=self.patient_two,
            doctor=self.second_doctor,
            disease=self.disease_a,
            diagnosis_date=date(2026, 4, 1),
            status="infected",
            weight=60,
            height=165,
            marital_status="married",
        )
        self.visit_b1 = Visit.objects.create(
            patient=self.patient_one,
            doctor=self.second_doctor,
            disease=self.disease_b,
            diagnosis_date=date(2026, 4, 2),
            status="infected",
            weight=70,
            height=175,
            marital_status="single",
        )

        GeoData.objects.create(
            patient=self.patient_one,
            visit=self.visit_a1,
            latitude=33.5001,
            longitude=36.2501,
            region_type="home",
        )
        GeoData.objects.create(
            patient=self.patient_two,
            visit=self.visit_a2,
            latitude=33.5301,
            longitude=36.2701,
            region_type="work",
        )
        GeoData.objects.create(
            patient=self.patient_one,
            visit=self.visit_b1,
            latitude=33.5401,
            longitude=36.2801,
            region_type="home",
        )
