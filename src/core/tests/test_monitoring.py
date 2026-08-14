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

class AutomaticMonitoringTests(CoreAPITestCase):
    def _create_cluster_seed_visit(self):
        nearby_visit = Visit.objects.create(
            patient=self.patient_two,
            doctor=self.second_doctor,
            disease=self.disease_a,
            diagnosis_date=date(2026, 4, 18),
            status="infected",
            weight=60,
            height=165,
            marital_status="married",
        )
        GeoData.objects.create(
            patient=self.patient_two,
            visit=nearby_visit,
            latitude=33.5005,
            longitude=36.2504,
            region_type="home",
        )
        return nearby_visit

    def test_monitoring_runs_automatically_after_geodata_create(self):
        self._create_cluster_seed_visit()
        current_visit = Visit.objects.create(
            patient=self.patient_one,
            doctor=self.doctor,
            disease=self.disease_a,
            diagnosis_date=date(2026, 4, 20),
            status="infected",
            weight=71,
            height=175,
            marital_status="single",
        )

        with self.captureOnCommitCallbacks(execute=True):
            GeoData.objects.create(
                patient=self.patient_one,
                visit=current_visit,
                latitude=33.5000,
                longitude=36.2500,
                region_type="home",
            )

        self.assertEqual(Report.objects.filter(disease=self.disease_a).count(), 1)
        self.assertEqual(GeoCluster.objects.filter(disease=self.disease_a).count(), 1)

    def test_monitoring_updates_existing_outputs_instead_of_creating_duplicates(self):
        self._create_cluster_seed_visit()
        current_visit = Visit.objects.create(
            patient=self.patient_one,
            doctor=self.doctor,
            disease=self.disease_a,
            diagnosis_date=date(2026, 4, 20),
            status="infected",
            weight=71,
            height=175,
            marital_status="single",
        )

        with self.captureOnCommitCallbacks(execute=True):
            current_geodata = GeoData.objects.create(
                patient=self.patient_one,
                visit=current_visit,
                latitude=33.5000,
                longitude=36.2500,
                region_type="home",
            )

        first_report = Report.objects.get(disease=self.disease_a)
        first_cluster = GeoCluster.objects.get(disease=self.disease_a)

        with self.captureOnCommitCallbacks(execute=True):
            current_geodata.latitude = 33.5001
            current_geodata.save()

        self.assertEqual(Report.objects.filter(disease=self.disease_a).count(), 1)
        self.assertEqual(GeoCluster.objects.filter(disease=self.disease_a).count(), 1)
        self.assertEqual(Report.objects.get(disease=self.disease_a).id, first_report.id)
        self.assertEqual(GeoCluster.objects.get(disease=self.disease_a).id, first_cluster.id)

    def test_monitoring_updates_active_report_for_followup_visit_in_same_outbreak(self):
        self._create_cluster_seed_visit()

        first_visit = Visit.objects.create(
            patient=self.patient_one,
            doctor=self.doctor,
            disease=self.disease_a,
            diagnosis_date=date(2026, 4, 20),
            status="infected",
            weight=71,
            height=175,
            marital_status="single",
        )
        with self.captureOnCommitCallbacks(execute=True):
            GeoData.objects.create(
                patient=self.patient_one,
                visit=first_visit,
                latitude=33.5000,
                longitude=36.2500,
                region_type="home",
            )

        second_visit = Visit.objects.create(
            patient=self.patient_one,
            doctor=self.doctor,
            disease=self.disease_a,
            diagnosis_date=date(2026, 4, 21),
            status="infected",
            weight=71,
            height=175,
            marital_status="single",
        )
        with self.captureOnCommitCallbacks(execute=True):
            GeoData.objects.create(
                patient=self.patient_one,
                visit=second_visit,
                latitude=33.5002,
                longitude=36.2501,
                region_type="home",
            )

        self.assertEqual(Report.objects.filter(disease=self.disease_a).count(), 1)
        report = Report.objects.get(disease=self.disease_a)
        self.assertGreaterEqual(report.current_case_count, 2)

class MonitoringVisibilityTests(CoreAPITestCase):
    def setUp(self):
        super().setUp()
        Report.objects.create(
            disease=self.disease_a,
            trigger_visit=self.visit_a1,
            analysis_period_start=date(2026, 4, 1),
            analysis_period_end=date(2026, 4, 2),
            alert_level="high",
            summary="Active measles alert",
            risk_score=55.0,
            nearby_case_count=1,
            current_case_count=2,
            previous_case_count=1,
            growth_rate=1.0,
            surge_ratio=2.0,
            reasons=["cluster"],
            status="new",
        )
        Report.objects.create(
            disease=self.disease_a,
            trigger_visit=self.visit_a2,
            analysis_period_start=date(2026, 4, 3),
            analysis_period_end=date(2026, 4, 4),
            alert_level="medium",
            summary="Reviewed measles alert",
            risk_score=35.0,
            nearby_case_count=1,
            current_case_count=2,
            previous_case_count=1,
            growth_rate=0.8,
            surge_ratio=1.8,
            reasons=["trend"],
            status="reviewed",
        )
        Report.objects.create(
            disease=self.disease_b,
            trigger_visit=self.visit_b1,
            analysis_period_start=date(2026, 4, 1),
            analysis_period_end=date(2026, 4, 2),
            alert_level="critical",
            summary="Resolved cholera alert",
            risk_score=90.0,
            nearby_case_count=2,
            current_case_count=3,
            previous_case_count=1,
            growth_rate=2.0,
            surge_ratio=3.0,
            reasons=["surge"],
            status="resolved",
        )
        Report.objects.create(
            disease=self.disease_b,
            trigger_visit=self.visit_b1,
            analysis_period_start=date(2026, 4, 2),
            analysis_period_end=date(2026, 4, 3),
            alert_level="low",
            summary="Low signal",
            risk_score=10.0,
            nearby_case_count=0,
            current_case_count=1,
            previous_case_count=1,
            growth_rate=0.0,
            surge_ratio=1.0,
            reasons=["low"],
            status="new",
        )

        GeoCluster.objects.create(
            center_lat=33.5000,
            center_long=36.2500,
            radius=1.5,
            disease=self.disease_a,
            case_count=4,
            risk_level=4,
        )
        GeoCluster.objects.create(
            center_lat=33.5400,
            center_long=36.2800,
            radius=0.8,
            disease=self.disease_b,
            case_count=1,
            risk_level=1,
        )

    def test_admin_can_view_active_alerts_only(self):
        self.client.force_authenticate(user=self.admin_user)

        response = self.client.get(reverse("report-active-alerts"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 2)
        self.assertEqual(response.json()[0]["alert_level"], "high")
        self.assertEqual(response.json()[0]["status"], "new")
        self.assertEqual(response.json()[0]["disease_code"], self.disease_a.disease_code)

    def test_active_alerts_can_be_filtered_by_disease(self):
        self.client.force_authenticate(user=self.admin_user)

        response = self.client.get(
            reverse("report-active-alerts"),
            {"disease": self.disease_b.id},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), [])

    def test_active_alerts_support_limit_and_min_risk_score_filters(self):
        self.client.force_authenticate(user=self.admin_user)

        response = self.client.get(
            reverse("report-active-alerts"),
            {"min_risk_score": 40, "limit": 1},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)
        self.assertGreaterEqual(response.json()[0]["risk_score"], 40)

    def test_admin_can_view_active_hotspots_only(self):
        self.client.force_authenticate(user=self.admin_user)

        response = self.client.get(reverse("geocluster-active-hotspots"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.json()[0]["risk_level"], 4)
        self.assertEqual(response.json()[0]["disease_code"], self.disease_a.disease_code)

    def test_active_hotspots_support_case_count_filter(self):
        self.client.force_authenticate(user=self.admin_user)

        response = self.client.get(
            reverse("geocluster-active-hotspots"),
            {"min_case_count": 5},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), [])

    def test_dashboard_summary_returns_operational_snapshot(self):
        self.client.force_authenticate(user=self.admin_user)

        response = self.client.get(reverse("report-dashboard-summary"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payload = response.json()
        self.assertEqual(payload["summary"]["active_alert_count"], 2)
        self.assertEqual(payload["summary"]["critical_alert_count"], 0)
        self.assertEqual(payload["summary"]["high_alert_count"], 1)
        self.assertEqual(payload["summary"]["medium_alert_count"], 1)
        self.assertEqual(payload["summary"]["new_alert_count"], 1)
        self.assertEqual(payload["summary"]["reviewed_alert_count"], 1)
        self.assertEqual(payload["summary"]["active_hotspot_count"], 1)
        self.assertEqual(payload["top_alert"]["disease"], self.disease_a.id)
        self.assertEqual(payload["top_hotspot"]["disease"], self.disease_a.id)
        self.assertEqual(payload["alerts_by_disease"][0]["disease__disease_code"], self.disease_a.disease_code)
