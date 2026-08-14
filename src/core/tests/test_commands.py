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

class DiseaseReferenceClassificationTests(APITestCase):
    def test_classify_reference_disease_marks_measles_as_high_priority_profile(self):
        classification = classify_reference_disease(
            disease_code="1F03",
            name="Measles",
            disease_type="epidemic_infectious",
            transmission_vector="airborne_droplet",
            risk_level=4,
            infection_score=0.92,
        )

        self.assertEqual(classification["policy_profile"], "high_priority")
        self.assertTrue(classification["high_priority"])
        self.assertFalse(classification["rare_disease"])

    def test_classify_reference_disease_marks_smallpox_as_rare_high_priority(self):
        classification = classify_reference_disease(
            disease_code="1E70",
            name="Smallpox",
            disease_type="high_alert_notifiable",
            transmission_vector="direct_contact_body_fluids",
            risk_level=5,
            infection_score=0.95,
        )

        self.assertEqual(classification["policy_profile"], "rare")
        self.assertTrue(classification["high_priority"])
        self.assertTrue(classification["rare_disease"])
        self.assertTrue(classification["is_reference"])

    def test_classify_reference_disease_marks_waterborne_gastro_cases_as_cluster_sensitive(self):
        classification = classify_reference_disease(
            disease_code="1A09.0",
            name="Salmonella enteritis",
            disease_type="gastrointestinal",
            transmission_vector="foodborne_waterborne_fecal_oral",
            risk_level=3,
            infection_score=0.55,
        )

        self.assertEqual(classification["policy_profile"], "cluster_sensitive")
        self.assertFalse(classification["rare_disease"])

    def test_classify_reference_disease_keeps_noninfectious_gastro_cases_general(self):
        classification = classify_reference_disease(
            disease_code="DA22.Z",
            name="Gastro-oesophageal reflux disease, unspecified",
            disease_type="gastrointestinal",
            transmission_vector="N/A",
            risk_level=2,
            infection_score=0.05,
        )

        self.assertEqual(classification["policy_profile"], "general")
        self.assertFalse(classification["high_priority"])

    def test_classify_reference_disease_marks_chronic_respiratory_signals_as_environmental(self):
        classification = classify_reference_disease(
            disease_code="CA23",
            name="Asthma",
            disease_type="respiratory",
            transmission_vector="air_pollution_smoke_exposure",
            risk_level=3,
            infection_score=0.1,
        )

        self.assertEqual(classification["policy_profile"], "environmental_signal")
        self.assertFalse(classification["rare_disease"])

class DiseaseImportCommandTests(APITestCase):
    def test_import_diseases_command_upserts_reference_diseases(self):
        with NamedTemporaryFile("w+", suffix=".csv", encoding="utf-8", delete=False) as temp_csv:
            temp_csv.write(
                "id;disease_code;name;type;transmission_vector;symptoms;risk_level;infection_score\n"
                "1;1A00;Cholera;epidemic_infectious;waterborne_foodborne_fecal_oral;diarrhea;4;0.82\n"
                "2;1F03;Measles;epidemic_infectious;airborne_droplet;fever;4;0.92\n"
            )
            temp_csv.flush()
            csv_path = temp_csv.name

        call_command("import_diseases", csv_path=csv_path)

        self.assertEqual(Disease.objects.count(), 2)
        cholera = Disease.objects.get(disease_code="1A00")
        self.assertEqual(cholera.source_name, REFERENCE_SOURCE_NAME)
        self.assertTrue(cholera.is_reference)
        self.assertEqual(cholera.policy_profile, "cluster_sensitive")

        with open(csv_path, "w", encoding="utf-8", newline="") as temp_csv:
            temp_csv.write(
                "id;disease_code;name;type;transmission_vector;symptoms;risk_level;infection_score\n"
                "1;1A00;Cholera updated;epidemic_infectious;waterborne_foodborne_fecal_oral;diarrhea;4;0.82\n"
                "2;1F03;Measles;epidemic_infectious;airborne_droplet;fever;4;0.92\n"
            )

        call_command("import_diseases", csv_path=csv_path)

        self.assertEqual(Disease.objects.count(), 2)
        self.assertEqual(Disease.objects.get(disease_code="1A00").name, "Cholera updated")

class AIDatasetExportCommandTests(CoreAPITestCase):
    def test_export_ai_dataset_command_writes_visit_and_daily_datasets(self):
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
        GeoCluster.objects.create(
            center_lat=33.5000,
            center_long=36.2500,
            radius=1.5,
            disease=self.disease_a,
            case_count=4,
            risk_level=4,
        )
        LabTest.objects.create(
            visit=self.visit_a1,
            test_code="CBC",
            test_name="Complete Blood Count",
            result="Abnormal high WBC",
            test_date="2026-04-01T10:00:00Z",
            notes="",
        )
        LabTest.objects.create(
            visit=self.visit_b1,
            test_code="ELEC",
            test_name="Electrolytes",
            result="Normal",
            test_date="2026-04-02T11:00:00Z",
            notes="",
        )

        output_dir = Path.cwd() / f"ai_export_test_{uuid4().hex}"
        output_dir.mkdir(parents=True, exist_ok=True)

        try:
            call_command("export_ai_dataset", output_dir=str(output_dir))

            visit_dataset_path = output_dir / "visit_level_dataset.csv"
            disease_day_dataset_path = output_dir / "disease_region_day_dataset.csv"

            self.assertTrue(visit_dataset_path.exists())
            self.assertTrue(disease_day_dataset_path.exists())

            with visit_dataset_path.open(encoding="utf-8-sig", newline="") as csv_file:
                visit_rows = list(csv.DictReader(csv_file))

            with disease_day_dataset_path.open(encoding="utf-8-sig", newline="") as csv_file:
                disease_day_rows = list(csv.DictReader(csv_file))

            self.assertEqual(len(visit_rows), 3)
            visit_a1_row = next(row for row in visit_rows if row["visit_id"] == str(self.visit_a1.id))
            self.assertEqual(visit_a1_row["disease_code"], self.disease_a.disease_code)
            self.assertEqual(visit_a1_row["primary_region_type"], "home")
            self.assertEqual(visit_a1_row["has_home_geodata"], "1")
            self.assertEqual(visit_a1_row["total_lab_test_count"], "1")
            self.assertEqual(visit_a1_row["abnormal_lab_test_count"], "1")
            self.assertEqual(visit_a1_row["has_active_alert"], "1")

            measles_home_row = next(
                row
                for row in disease_day_rows
                if row["disease_code"] == self.disease_a.disease_code
                and row["diagnosis_date"] == "2026-04-01"
                and row["region_type"] == "home"
            )
            self.assertEqual(measles_home_row["visit_count"], "1")
            self.assertEqual(measles_home_row["unique_patient_count"], "1")
            self.assertEqual(measles_home_row["total_lab_test_count"], "1")
            self.assertEqual(measles_home_row["abnormal_lab_test_count"], "1")
            self.assertEqual(measles_home_row["active_report_count"], "1")
            self.assertEqual(measles_home_row["high_alert_count"], "1")
            self.assertEqual(measles_home_row["active_hotspot_count"], "1")
        finally:
            shutil.rmtree(output_dir, ignore_errors=True)
