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

class OutbreakEngineServiceTests(CoreAPITestCase):
    def _build_context(self, visit: Visit, geodata: GeoData) -> VisitOutbreakContext:
        return VisitOutbreakContext(
            visit_id=visit.id,
            patient_id=visit.patient_id,
            doctor_id=visit.doctor_id,
            disease_id=visit.disease_id,
            disease_code=visit.disease.disease_code,
            diagnosis_date=visit.diagnosis_date,
            latitude=geodata.latitude,
            longitude=geodata.longitude,
            region_type=geodata.region_type,
        )

    def test_rare_disease_single_case_triggers_critical_alert(self):
        polio = Disease.objects.create(
            disease_code="POL",
            name="Polio",
            type="viral",
            transmission_vector="contact",
            symptoms="weakness",
            risk_level=5,
            infection_score=4.0,
        )
        visit = Visit.objects.create(
            patient=self.patient_one,
            doctor=self.doctor,
            disease=polio,
            diagnosis_date=date(2026, 4, 12),
            status="infected",
            weight=70,
            height=175,
            marital_status="single",
        )
        geodata = GeoData.objects.create(
            patient=self.patient_one,
            visit=visit,
            latitude=33.5000,
            longitude=36.2500,
            region_type="home",
        )

        policy = get_policy_for_disease(
            disease_code=polio.disease_code,
            risk_level=polio.risk_level,
            infection_score=polio.infection_score,
        )
        analysis = evaluate_visit_outbreak(
            context=self._build_context(visit, geodata),
            policy=policy,
        )

        self.assertEqual(analysis.alert_level, "critical")
        self.assertTrue(analysis.should_create_report)
        self.assertFalse(analysis.should_create_cluster)
        self.assertEqual(analysis.nearby_case_count, 0)
        self.assertIn("Rare-disease policy is active.", analysis.reasons)
        self.assertEqual(analysis.metadata["local_case_count"], 1)

    def test_high_priority_cluster_creates_high_or_critical_alert(self):
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
        current_geodata = GeoData.objects.create(
            patient=self.patient_one,
            visit=current_visit,
            latitude=33.5000,
            longitude=36.2500,
            region_type="home",
        )

        policy = get_policy_for_disease(
            disease_code=self.disease_a.disease_code,
            risk_level=self.disease_a.risk_level,
            infection_score=self.disease_a.infection_score,
        )
        analysis = evaluate_visit_outbreak(
            context=self._build_context(current_visit, current_geodata),
            policy=policy,
        )

        self.assertIn(analysis.alert_level, {"high", "critical"})
        self.assertTrue(analysis.should_create_report)
        self.assertTrue(analysis.should_create_cluster)
        self.assertEqual(analysis.nearby_case_count, 1)
        self.assertEqual(analysis.matched_case_ids, [nearby_visit.id])
        self.assertIn("High-priority disease policy is active.", analysis.reasons)

    def test_generic_isolated_case_stays_low_and_does_not_create_outputs(self):
        flu = Disease.objects.create(
            disease_code="FLU",
            name="Seasonal Flu",
            type="viral",
            transmission_vector="airborne",
            symptoms="fever",
            risk_level=1,
            infection_score=1.0,
        )
        visit = Visit.objects.create(
            patient=self.patient_two,
            doctor=self.second_doctor,
            disease=flu,
            diagnosis_date=date(2026, 4, 12),
            status="infected",
            weight=62,
            height=166,
            marital_status="married",
        )
        geodata = GeoData.objects.create(
            patient=self.patient_two,
            visit=visit,
            latitude=33.5300,
            longitude=36.2700,
            region_type="work",
        )

        policy = get_policy_for_disease(
            disease_code=flu.disease_code,
            risk_level=flu.risk_level,
            infection_score=flu.infection_score,
        )
        analysis = evaluate_visit_outbreak(
            context=self._build_context(visit, geodata),
            policy=policy,
        )

        self.assertEqual(analysis.alert_level, "low")
        self.assertFalse(analysis.should_create_report)
        self.assertFalse(analysis.should_create_cluster)
        self.assertEqual(analysis.nearby_case_count, 0)
        self.assertLess(analysis.score, 35)

    def test_engine_returns_explainable_score_breakdown_metadata(self):
        current_visit = Visit.objects.create(
            patient=self.patient_one,
            doctor=self.doctor,
            disease=self.disease_b,
            diagnosis_date=date(2026, 4, 12),
            status="infected",
            weight=70,
            height=175,
            marital_status="single",
        )
        current_geodata = GeoData.objects.create(
            patient=self.patient_one,
            visit=current_visit,
            latitude=33.5400,
            longitude=36.2800,
            region_type="home",
        )

        policy = get_policy_for_disease(
            disease_code=self.disease_b.disease_code,
            risk_level=self.disease_b.risk_level,
            infection_score=self.disease_b.infection_score,
        )
        analysis = evaluate_visit_outbreak(
            context=self._build_context(current_visit, current_geodata),
            policy=policy,
        )

        self.assertIn("score_breakdown", analysis.metadata)
        self.assertIn("severity_points", analysis.metadata["score_breakdown"])
        self.assertIn("density_points", analysis.metadata["score_breakdown"])
        self.assertIn("trend_points", analysis.metadata["score_breakdown"])
        self.assertIn("current_window", analysis.metadata)
        self.assertIn("previous_window", analysis.metadata)

    def test_sudden_surge_triggers_alert_even_without_local_cluster(self):
        current_visit_one = Visit.objects.create(
            patient=self.patient_two,
            doctor=self.second_doctor,
            disease=self.disease_b,
            diagnosis_date=date(2026, 4, 18),
            status="infected",
            weight=61,
            height=165,
            marital_status="married",
        )
        GeoData.objects.create(
            patient=self.patient_two,
            visit=current_visit_one,
            latitude=33.6100,
            longitude=36.3600,
            region_type="home",
        )

        patient_three = Patient.objects.create(
            national_number="3003",
            name="Surge Patient",
            birth_date=date(1991, 7, 7),
            gender="male",
            residence_lat=33.6600,
            residence_long=36.4200,
            work_lat=33.6610,
            work_long=36.4210,
        )
        current_visit_two = Visit.objects.create(
            patient=patient_three,
            doctor=self.doctor,
            disease=self.disease_b,
            diagnosis_date=date(2026, 4, 19),
            status="infected",
            weight=69,
            height=171,
            marital_status="single",
        )
        GeoData.objects.create(
            patient=patient_three,
            visit=current_visit_two,
            latitude=33.6600,
            longitude=36.4200,
            region_type="work",
        )

        current_visit = Visit.objects.create(
            patient=self.patient_one,
            doctor=self.doctor,
            disease=self.disease_b,
            diagnosis_date=date(2026, 4, 20),
            status="infected",
            weight=70,
            height=175,
            marital_status="single",
        )
        current_geodata = GeoData.objects.create(
            patient=self.patient_one,
            visit=current_visit,
            latitude=33.5400,
            longitude=36.2800,
            region_type="home",
        )

        policy = get_policy_for_disease(
            disease_code=self.disease_b.disease_code,
            risk_level=self.disease_b.risk_level,
            infection_score=self.disease_b.infection_score,
        )
        analysis = evaluate_visit_outbreak(
            context=self._build_context(current_visit, current_geodata),
            policy=policy,
        )

        self.assertGreaterEqual(analysis.trend.current_count, 3)
        self.assertEqual(analysis.nearby_case_count, 0)
        self.assertGreaterEqual(analysis.trend.surge_ratio, 3.0)
        self.assertIn(analysis.alert_level, {"medium", "high", "critical"})
        self.assertTrue(
            any("surge" in reason.lower() or "growth" in reason.lower() for reason in analysis.reasons)
        )

class PersistenceServiceTests(CoreAPITestCase):
    def _build_context(self, visit: Visit, geodata: GeoData) -> VisitOutbreakContext:
        return VisitOutbreakContext(
            visit_id=visit.id,
            patient_id=visit.patient_id,
            doctor_id=visit.doctor_id,
            disease_id=visit.disease_id,
            disease_code=visit.disease.disease_code,
            diagnosis_date=visit.diagnosis_date,
            latitude=geodata.latitude,
            longitude=geodata.longitude,
            region_type=geodata.region_type,
        )

    def test_create_report_from_analysis_persists_report(self):
        polio = Disease.objects.create(
            disease_code="POL",
            name="Polio",
            type="viral",
            transmission_vector="contact",
            symptoms="weakness",
            risk_level=5,
            infection_score=4.0,
        )
        visit = Visit.objects.create(
            patient=self.patient_one,
            doctor=self.doctor,
            disease=polio,
            diagnosis_date=date(2026, 4, 12),
            status="infected",
            weight=70,
            height=175,
            marital_status="single",
        )
        geodata = GeoData.objects.create(
            patient=self.patient_one,
            visit=visit,
            latitude=33.5000,
            longitude=36.2500,
            region_type="home",
        )

        policy = get_policy_for_disease(
            disease_code=polio.disease_code,
            risk_level=polio.risk_level,
            infection_score=polio.infection_score,
        )
        analysis = evaluate_visit_outbreak(
            context=self._build_context(visit, geodata),
            policy=policy,
        )

        report_id = create_report_from_analysis(
            context=self._build_context(visit, geodata),
            analysis=analysis,
        )
        report = Report.objects.get(id=report_id)

        self.assertEqual(report.disease_id, polio.id)
        self.assertEqual(report.trigger_visit_id, visit.id)
        self.assertEqual(report.alert_level, "critical")
        self.assertEqual(report.nearby_case_count, 0)
        self.assertEqual(report.current_case_count, analysis.trend.current_count)
        self.assertEqual(report.reasons, analysis.reasons)

    def test_create_report_from_analysis_updates_existing_report_for_same_trigger_visit(self):
        polio = Disease.objects.create(
            disease_code="POL",
            name="Polio",
            type="viral",
            transmission_vector="contact",
            symptoms="weakness",
            risk_level=5,
            infection_score=4.0,
        )
        visit = Visit.objects.create(
            patient=self.patient_one,
            doctor=self.doctor,
            disease=polio,
            diagnosis_date=date(2026, 4, 12),
            status="infected",
            weight=70,
            height=175,
            marital_status="single",
        )
        geodata = GeoData.objects.create(
            patient=self.patient_one,
            visit=visit,
            latitude=33.5000,
            longitude=36.2500,
            region_type="home",
        )

        context = self._build_context(visit, geodata)
        policy = get_policy_for_disease(
            disease_code=polio.disease_code,
            risk_level=polio.risk_level,
            infection_score=polio.infection_score,
        )
        analysis = evaluate_visit_outbreak(context=context, policy=policy)

        first_report_id = create_report_from_analysis(context=context, analysis=analysis)
        second_report_id = create_report_from_analysis(context=context, analysis=analysis)

        self.assertEqual(first_report_id, second_report_id)
        self.assertEqual(Report.objects.filter(trigger_visit=visit).count(), 1)

    def test_create_cluster_from_analysis_persists_cluster(self):
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
        current_geodata = GeoData.objects.create(
            patient=self.patient_one,
            visit=current_visit,
            latitude=33.5000,
            longitude=36.2500,
            region_type="home",
        )

        policy = get_policy_for_disease(
            disease_code=self.disease_a.disease_code,
            risk_level=self.disease_a.risk_level,
            infection_score=self.disease_a.infection_score,
        )
        analysis = evaluate_visit_outbreak(
            context=self._build_context(current_visit, current_geodata),
            policy=policy,
        )

        cluster_id = create_cluster_from_analysis(
            context=self._build_context(current_visit, current_geodata),
            analysis=analysis,
        )
        cluster = GeoCluster.objects.get(id=cluster_id)

        self.assertEqual(cluster.disease_id, self.disease_a.id)
        self.assertEqual(cluster.case_count, analysis.metadata["local_case_count"])
        self.assertEqual(cluster.risk_level, ALERT_LEVEL_TO_RISK_LEVEL[analysis.alert_level])
        self.assertGreater(cluster.radius, 0)

    def test_create_cluster_from_analysis_rejects_non_cluster_even_if_cluster_exists_nearby(self):
        GeoCluster.objects.create(
            center_lat=33.5000,
            center_long=36.2500,
            radius=1.0,
            disease=self.disease_b,
            case_count=3,
            risk_level=4,
        )
        context = VisitOutbreakContext(
            visit_id=self.visit_b1.id,
            patient_id=self.patient_one.id,
            doctor_id=self.second_doctor.id,
            disease_id=self.disease_b.id,
            disease_code=self.disease_b.disease_code,
            diagnosis_date=self.visit_b1.diagnosis_date,
            latitude=33.5001,
            longitude=36.2501,
            region_type="home",
        )
        analysis = OutbreakAnalysis(
            alert_level="low",
            score=12.0,
            should_create_cluster=False,
            metadata={"local_case_count": 1},
        )

        with self.assertRaises(ValueError):
            create_cluster_from_analysis(context=context, analysis=analysis)

class EvaluateOutbreakEndpointPersistenceTests(CoreAPITestCase):
    def _create_measles_cluster_case(self):
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
        GeoData.objects.create(
            patient=self.patient_one,
            visit=current_visit,
            latitude=33.5000,
            longitude=36.2500,
            region_type="home",
        )
        return current_visit

    def test_admin_can_persist_report_and_cluster_from_evaluation_endpoint(self):
        current_visit = self._create_measles_cluster_case()
        self.client.force_authenticate(user=self.admin_user)

        response = self.client.post(
            reverse("visit-evaluate-outbreak", args=[current_visit.id]),
            {"save_report": True, "save_cluster": True},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.json()["persistence"]["report_saved"])
        self.assertTrue(response.json()["persistence"]["cluster_saved"])
        self.assertIsNotNone(response.json()["persistence"]["report_id"])
        self.assertIsNotNone(response.json()["persistence"]["cluster_id"])
        self.assertTrue(Report.objects.filter(id=response.json()["persistence"]["report_id"]).exists())
        self.assertTrue(GeoCluster.objects.filter(id=response.json()["persistence"]["cluster_id"]).exists())

    def test_doctor_cannot_persist_outputs_from_evaluation_endpoint(self):
        current_visit = self._create_measles_cluster_case()
        self.client.force_authenticate(user=self.doctor_user)

        response = self.client.post(
            reverse("visit-evaluate-outbreak", args=[current_visit.id]),
            {"save_report": True},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_exposure_outbreak_endpoint_analyzes_all_visit_geodata_points(self):
        current_visit = self._create_measles_cluster_case()
        GeoData.objects.create(
            patient=self.patient_one,
            visit=current_visit,
            latitude=33.6000,
            longitude=36.3500,
            region_type="work",
        )
        self.client.force_authenticate(user=self.doctor_user)

        response = self.client.get(
            reverse("visit-evaluate-exposure-outbreak", args=[current_visit.id])
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payload = response.json()
        self.assertEqual(payload["visit_id"], current_visit.id)
        self.assertEqual(len(payload["results"]), 2)
        self.assertEqual(
            {result["region_type"] for result in payload["results"]},
            {"home", "work"},
        )
        self.assertTrue(
            all(result["persistence"]["report_saved"] is False for result in payload["results"])
        )

    def test_doctor_cannot_persist_exposure_outbreak_outputs(self):
        current_visit = self._create_measles_cluster_case()
        self.client.force_authenticate(user=self.doctor_user)

        response = self.client.post(
            reverse("visit-evaluate-exposure-outbreak", args=[current_visit.id]),
            {"save_report": True},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

class MissingDataScenarioTests(CoreAPITestCase):
    def test_evaluate_outbreak_returns_clear_error_when_geodata_is_missing(self):
        visit_without_geodata = Visit.objects.create(
            patient=self.patient_one,
            doctor=self.doctor,
            disease=self.disease_a,
            diagnosis_date=date(2026, 4, 12),
            status="infected",
            weight=70,
            height=175,
            marital_status="single",
        )
        self.client.force_authenticate(user=self.doctor_user)

        response = self.client.get(
            reverse("visit-evaluate-outbreak", args=[visit_without_geodata.id])
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.json()["error"],
            "No GeoData record was found for this visit.",
        )

    def test_exposure_outbreak_returns_clear_error_when_geodata_is_missing(self):
        visit_without_geodata = Visit.objects.create(
            patient=self.patient_one,
            doctor=self.doctor,
            disease=self.disease_a,
            diagnosis_date=date(2026, 4, 12),
            status="infected",
            weight=70,
            height=175,
            marital_status="single",
        )
        self.client.force_authenticate(user=self.doctor_user)

        response = self.client.get(
            reverse("visit-evaluate-exposure-outbreak", args=[visit_without_geodata.id])
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.json()["error"],
            "No GeoData records were found for this visit.",
        )
