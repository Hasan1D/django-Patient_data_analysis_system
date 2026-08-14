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
from .test_base import (
    _FakeNumpy,
    _FakeHDBSCANModule,
    _fake_ml_dependencies,
    _unlink_with_retries,
)

class DBSCANHotspotTests(CoreAPITestCase):
    def _seed_measles_dbscan_points(self):
        points = []
        visit_specs = [
            (self.patient_one, self.doctor, date(2026, 4, 10), 33.5000, 36.2500, "home"),
            (self.patient_two, self.second_doctor, date(2026, 4, 11), 33.5006, 36.2504, "home"),
        ]

        patient_three = Patient.objects.create(
            national_number="3007",
            name="Cluster Patient Three",
            birth_date=date(1994, 8, 8),
            gender="male",
            residence_lat=33.5010,
            residence_long=36.2506,
            work_lat=33.5015,
            work_long=36.2510,
        )
        visit_specs.append((patient_three, self.doctor, date(2026, 4, 12), 33.5010, 36.2506, "home"))

        patient_four = Patient.objects.create(
            national_number="3008",
            name="Noise Patient",
            birth_date=date(1995, 9, 9),
            gender="female",
            residence_lat=33.6200,
            residence_long=36.4200,
            work_lat=33.6210,
            work_long=36.4210,
        )
        visit_specs.append((patient_four, self.doctor, date(2026, 4, 12), 33.6200, 36.4200, "home"))

        for patient, doctor, diagnosis_date, latitude, longitude, region_type in visit_specs:
            visit = Visit.objects.create(
                patient=patient,
                doctor=doctor,
                disease=self.disease_a,
                diagnosis_date=diagnosis_date,
                status="infected",
                weight=70,
                height=175,
                marital_status="single",
            )
            geodata = GeoData.objects.create(
                patient=patient,
                visit=visit,
                latitude=latitude,
                longitude=longitude,
                region_type=region_type,
            )
            points.append((visit, geodata))

        return points

    def test_detect_dbscan_clusters_finds_local_cluster_and_ignores_noise(self):
        seeded_points = self._seed_measles_dbscan_points()

        clusters = detect_dbscan_clusters(
            disease_id=self.disease_a.id,
            lookback_days=7,
            eps_km=1.0,
            min_samples=2,
        )

        self.assertEqual(len(clusters), 1)
        cluster = clusters[0]
        self.assertEqual(cluster.disease_id, self.disease_a.id)
        self.assertEqual(cluster.point_count, 3)
        self.assertEqual(len(cluster.member_geodata_ids), 3)
        self.assertEqual(len(cluster.member_visit_ids), 3)
        self.assertGreater(cluster.radius_km, 0)
        self.assertTrue(
            all(
                visit.id in cluster.member_visit_ids
                for visit, _ in seeded_points[:3]
            )
        )
        self.assertNotIn(seeded_points[3][0].id, cluster.member_visit_ids)

    def test_dbscan_exposure_mode_uses_home_and_work_points_and_case_mode_deduplicates(self):
        patient = Patient.objects.create(
            national_number="3010",
            name="Exposure DBSCAN Patient",
            birth_date=date(1994, 8, 8),
            gender="male",
            residence_lat=33.5100,
            residence_long=36.2600,
            work_lat=33.5103,
            work_long=36.2603,
        )
        visit = Visit.objects.create(
            patient=patient,
            doctor=self.doctor,
            disease=self.disease_a,
            diagnosis_date=date(2026, 4, 13),
            status="infected",
            weight=70,
            height=175,
            marital_status="single",
        )
        GeoData.objects.create(
            patient=patient,
            visit=visit,
            latitude=33.5100,
            longitude=36.2600,
            region_type="home",
        )
        GeoData.objects.create(
            patient=patient,
            visit=visit,
            latitude=33.5103,
            longitude=36.2603,
            region_type="work",
        )

        exposure_clusters = detect_dbscan_clusters(
            disease_id=self.disease_a.id,
            date_from=date(2026, 4, 13),
            date_to=date(2026, 4, 13),
            eps_km=1.0,
            min_samples=2,
        )
        case_clusters = detect_dbscan_clusters(
            disease_id=self.disease_a.id,
            date_from=date(2026, 4, 13),
            date_to=date(2026, 4, 13),
            eps_km=1.0,
            min_samples=2,
            point_mode="case",
        )

        self.assertEqual(len(exposure_clusters), 1)
        self.assertEqual(exposure_clusters[0].point_count, 2)
        self.assertEqual(exposure_clusters[0].unique_visit_count, 1)
        self.assertEqual(exposure_clusters[0].unique_patient_count, 1)
        self.assertEqual(set(exposure_clusters[0].region_types), {"home", "work"})
        self.assertEqual(case_clusters, [])

    def test_dbscan_rejects_invalid_point_mode(self):
        with self.assertRaises(ValueError):
            detect_dbscan_clusters(
                disease_id=self.disease_a.id,
                point_mode="patient",
            )

    def test_persist_dbscan_clusters_creates_and_updates_geoclusters(self):
        self._seed_measles_dbscan_points()
        clusters = detect_dbscan_clusters(
            disease_id=self.disease_a.id,
            lookback_days=7,
            eps_km=1.0,
            min_samples=2,
        )

        first_ids = persist_dbscan_clusters(clusters=clusters)
        second_ids = persist_dbscan_clusters(clusters=clusters)

        self.assertEqual(len(first_ids), 1)
        self.assertEqual(first_ids, second_ids)
        self.assertEqual(GeoCluster.objects.filter(disease=self.disease_a).count(), 1)

    def test_admin_can_detect_dbscan_clusters_from_api(self):
        self._seed_measles_dbscan_points()
        self.client.force_authenticate(user=self.admin_user)

        response = self.client.post(
            reverse("geocluster-detect-dbscan"),
            {
                "disease": self.disease_a.id,
                "lookback_days": 7,
                "eps_km": 1.0,
                "min_samples": 2,
                "persist": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payload = response.json()
        self.assertEqual(payload["cluster_count"], 1)
        self.assertEqual(payload["parameters"]["point_mode"], "exposure")
        self.assertEqual(len(payload["clusters"]), 1)
        self.assertEqual(len(payload["persisted_cluster_ids"]), 1)
        self.assertEqual(GeoCluster.objects.filter(disease=self.disease_a).count(), 1)

class HDBSCANHotspotTests(CoreAPITestCase):
    def _create_patient(self, index: int) -> Patient:
        return Patient.objects.create(
            national_number=f"8{index:04d}",
            name=f"HDBSCAN Patient {index}",
            birth_date=date(1990, 1, 1),
            gender="male" if index % 2 else "female",
            residence_lat=33.80 + (index * 0.0001),
            residence_long=36.80 + (index * 0.0001),
            work_lat=33.85 + (index * 0.0001),
            work_long=36.85 + (index * 0.0001),
        )

    def _create_visit(
        self,
        *,
        patient,
        disease,
        diagnosis_date,
        latitude,
        longitude,
        status_value="infected",
    ) -> Visit:
        visit = Visit.objects.create(
            patient=patient,
            doctor=self.doctor,
            disease=disease,
            diagnosis_date=diagnosis_date,
            status=status_value,
            weight=70,
            height=175,
            marital_status="single",
        )
        GeoData.objects.create(
            patient=patient,
            visit=visit,
            latitude=latitude,
            longitude=longitude,
            region_type="home",
        )
        return visit

    def _patch_hdbscan(self, labels, probabilities):
        return patch(
            "core.services.hdbscan_hotspots._load_hdbscan_dependencies",
            return_value=(_FakeNumpy, _FakeHDBSCANModule(labels, probabilities)),
        )

    def test_hdbscan_uses_active_latest_cases_without_cured_or_followup_duplication(self):
        followup_patient = self._create_patient(1)
        active_patient_one = self._create_patient(2)
        active_patient_two = self._create_patient(3)
        cured_patient = self._create_patient(4)

        old_followup = self._create_visit(
            patient=followup_patient,
            disease=self.disease_a,
            diagnosis_date=date(2026, 4, 10),
            latitude=33.8000,
            longitude=36.8000,
        )
        latest_followup = self._create_visit(
            patient=followup_patient,
            disease=self.disease_a,
            diagnosis_date=date(2026, 4, 11),
            latitude=33.8001,
            longitude=36.8001,
        )
        active_visit_one = self._create_visit(
            patient=active_patient_one,
            disease=self.disease_a,
            diagnosis_date=date(2026, 4, 11),
            latitude=33.8002,
            longitude=36.8002,
        )
        active_visit_two = self._create_visit(
            patient=active_patient_two,
            disease=self.disease_a,
            diagnosis_date=date(2026, 4, 11),
            latitude=33.8003,
            longitude=36.8003,
        )
        cured_infected = self._create_visit(
            patient=cured_patient,
            disease=self.disease_a,
            diagnosis_date=date(2026, 4, 10),
            latitude=33.8004,
            longitude=36.8004,
        )
        cured_latest = self._create_visit(
            patient=cured_patient,
            disease=self.disease_a,
            diagnosis_date=date(2026, 4, 12),
            latitude=33.8005,
            longitude=36.8005,
            status_value="cured",
        )

        with self._patch_hdbscan(labels=[0, 0, 0], probabilities=[0.9, 0.8, 0.7]):
            result = detect_hdbscan_clusters(
                disease_id=self.disease_a.id,
                date_from=date(2026, 4, 9),
                date_to=date(2026, 4, 12),
                min_cluster_size=3,
            )

        self.assertEqual(len(result.clusters), 1)
        cluster = result.clusters[0]
        self.assertEqual(cluster.point_count, 3)
        self.assertEqual(
            set(cluster.member_visit_ids),
            {latest_followup.id, active_visit_one.id, active_visit_two.id},
        )
        self.assertNotIn(old_followup.id, cluster.member_visit_ids)
        self.assertNotIn(cured_infected.id, cluster.member_visit_ids)
        self.assertNotIn(cured_latest.id, cluster.member_visit_ids)
        self.assertEqual(cluster.probability, 0.8)

    def test_hdbscan_exposure_mode_uses_home_and_work_points_and_case_mode_deduplicates(self):
        patient = self._create_patient(50)
        visit = self._create_visit(
            patient=patient,
            disease=self.disease_a,
            diagnosis_date=date(2026, 4, 14),
            latitude=33.8200,
            longitude=36.8200,
        )
        GeoData.objects.create(
            patient=patient,
            visit=visit,
            latitude=33.8202,
            longitude=36.8202,
            region_type="work",
        )

        with self._patch_hdbscan(labels=[0, 0], probabilities=[0.9, 0.7]):
            exposure_result = detect_hdbscan_clusters(
                disease_id=self.disease_a.id,
                date_from=date(2026, 4, 14),
                date_to=date(2026, 4, 14),
                min_cluster_size=2,
            )
        case_result = detect_hdbscan_clusters(
            disease_id=self.disease_a.id,
            date_from=date(2026, 4, 14),
            date_to=date(2026, 4, 14),
            min_cluster_size=2,
            point_mode="case",
        )

        self.assertEqual(len(exposure_result.clusters), 1)
        cluster = exposure_result.clusters[0]
        self.assertEqual(cluster.point_count, 2)
        self.assertEqual(cluster.unique_visit_count, 1)
        self.assertEqual(cluster.unique_patient_count, 1)
        self.assertEqual(set(cluster.region_types), {"home", "work"})
        self.assertEqual(case_result.clusters, [])
        self.assertEqual(case_result.noise_count, 1)

    def test_hdbscan_rejects_invalid_point_mode(self):
        with self.assertRaises(ValueError):
            detect_hdbscan_clusters(
                disease_id=self.disease_a.id,
                point_mode="patient",
            )

    def test_hdbscan_excludes_in_window_infected_when_latest_visit_is_later_cured(self):
        active_patient_one = self._create_patient(5)
        active_patient_two = self._create_patient(6)
        later_cured_patient = self._create_patient(7)

        self._create_visit(
            patient=active_patient_one,
            disease=self.disease_a,
            diagnosis_date=date(2026, 4, 11),
            latitude=33.8010,
            longitude=36.8010,
        )
        self._create_visit(
            patient=active_patient_two,
            disease=self.disease_a,
            diagnosis_date=date(2026, 4, 11),
            latitude=33.8011,
            longitude=36.8011,
        )
        in_window_infected = self._create_visit(
            patient=later_cured_patient,
            disease=self.disease_a,
            diagnosis_date=date(2026, 4, 11),
            latitude=33.8012,
            longitude=36.8012,
        )
        later_cured = self._create_visit(
            patient=later_cured_patient,
            disease=self.disease_a,
            diagnosis_date=date(2026, 4, 13),
            latitude=33.8013,
            longitude=36.8013,
            status_value="cured",
        )

        result = detect_hdbscan_clusters(
            disease_id=self.disease_a.id,
            date_from=date(2026, 4, 9),
            date_to=date(2026, 4, 12),
            min_cluster_size=3,
        )

        self.assertEqual(result.clusters, [])
        self.assertEqual(result.noise_count, 2)
        self.assertEqual(
            set(
                Visit.objects.filter(
                    id__in=[in_window_infected.id, later_cured.id],
                    status="infected",
                ).values_list("id", flat=True)
            ),
            {in_window_infected.id},
        )

    def test_hdbscan_noise_is_not_persisted_as_geocluster(self):
        for index in range(10, 13):
            self._create_visit(
                patient=self._create_patient(index),
                disease=self.disease_b,
                diagnosis_date=date(2026, 4, 11),
                latitude=33.9000 + (index * 0.0001),
                longitude=36.9000 + (index * 0.0001),
            )

        with self._patch_hdbscan(labels=[-1, -1, -1], probabilities=[0.1, 0.2, 0.3]):
            result = detect_hdbscan_clusters(
                disease_id=self.disease_b.id,
                date_from=date(2026, 4, 9),
                date_to=date(2026, 4, 12),
                min_cluster_size=3,
            )

        self.assertEqual(result.clusters, [])
        self.assertEqual(result.noise_count, 3)
        self.assertEqual(persist_hdbscan_clusters(clusters=result.clusters), [])
        self.assertEqual(GeoCluster.objects.filter(disease=self.disease_b).count(), 0)

    def test_admin_can_detect_hdbscan_clusters_from_api(self):
        for index in range(20, 23):
            self._create_visit(
                patient=self._create_patient(index),
                disease=self.disease_a,
                diagnosis_date=date(2026, 4, 11),
                latitude=33.8100 + (index * 0.0001),
                longitude=36.8100 + (index * 0.0001),
            )
        self.client.force_authenticate(user=self.admin_user)

        with self._patch_hdbscan(labels=[0, 0, 0], probabilities=[0.6, 0.7, 0.8]):
            response = self.client.post(
                reverse("geocluster-detect-hdbscan"),
                {
                    "disease": self.disease_a.id,
                    "date_from": "2026-04-09",
                    "date_to": "2026-04-12",
                    "min_cluster_size": 3,
                    "persist": True,
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payload = response.json()
        self.assertEqual(payload["cluster_count"], 1)
        self.assertEqual(payload["noise_count"], 0)
        self.assertEqual(payload["parameters"]["point_mode"], "exposure")
        self.assertEqual(len(payload["clusters"]), 1)
        self.assertEqual(len(payload["persisted_ids"]), 1)
        self.assertEqual(len(payload["persisted_cluster_ids"]), 1)
        self.assertEqual(GeoCluster.objects.filter(disease=self.disease_a).count(), 1)

class AnomalyDetectionTests(CoreAPITestCase):
    def _create_case(
        self,
        *,
        patient,
        doctor,
        disease,
        diagnosis_date,
        latitude,
        longitude,
        region_type="home",
    ):
        visit = Visit.objects.create(
            patient=patient,
            doctor=doctor,
            disease=disease,
            diagnosis_date=diagnosis_date,
            status="infected",
            weight=70,
            height=175,
            marital_status="single",
        )
        GeoData.objects.create(
            patient=patient,
            visit=visit,
            latitude=latitude,
            longitude=longitude,
            region_type=region_type,
        )
        return visit

    def _create_extra_patient(self, index: int) -> Patient:
        return Patient.objects.create(
            national_number=f"4{index:03d}",
            name=f"Anomaly Patient {index}",
            birth_date=date(1990, 1, 1),
            gender="male" if index % 2 else "female",
            residence_lat=33.50 + (index * 0.001),
            residence_long=36.25 + (index * 0.001),
            work_lat=33.55 + (index * 0.001),
            work_long=36.30 + (index * 0.001),
        )

    def test_detect_temporal_anomalies_flags_daily_spike(self):
        stable_dates = [date(2026, 4, 5), date(2026, 4, 6), date(2026, 4, 7), date(2026, 4, 8)]
        for index, current_date in enumerate(stable_dates, start=1):
            patient = self._create_extra_patient(index)
            self._create_case(
                patient=patient,
                doctor=self.doctor,
                disease=self.disease_b,
                diagnosis_date=current_date,
                latitude=33.5400 + (index * 0.0002),
                longitude=36.2800 + (index * 0.0002),
            )

        spike_date = date(2026, 4, 9)
        for index in range(5, 9):
            patient = self._create_extra_patient(index)
            self._create_case(
                patient=patient,
                doctor=self.second_doctor,
                disease=self.disease_b,
                diagnosis_date=spike_date,
                latitude=33.5405 + (index * 0.0002),
                longitude=36.2805 + (index * 0.0002),
            )

        candidates = detect_temporal_anomalies(
            disease_id=self.disease_b.id,
            lookback_days=14,
            baseline_window_days=4,
            region_type="home",
        )

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.disease_id, self.disease_b.id)
        self.assertEqual(candidate.target_date, "2026-04-09")
        self.assertEqual(candidate.observed_count, 4)
        self.assertGreaterEqual(candidate.surge_ratio, 4.0)
        self.assertIn(candidate.severity, {"high", "critical"})

    def test_detect_temporal_anomalies_ignores_stable_series(self):
        stable_disease = Disease.objects.create(
            disease_code="STABLE",
            name="Stable Disease",
            type="viral",
            transmission_vector="airborne",
            symptoms="fever",
            risk_level=2,
            infection_score=0.4,
        )
        for index, current_date in enumerate(
            [date(2026, 4, 5), date(2026, 4, 6), date(2026, 4, 7), date(2026, 4, 8), date(2026, 4, 9)],
            start=1,
        ):
            patient = self._create_extra_patient(index + 20)
            self._create_case(
                patient=patient,
                doctor=self.doctor,
                disease=stable_disease,
                diagnosis_date=current_date,
                latitude=33.5000 + (index * 0.0001),
                longitude=36.2500 + (index * 0.0001),
            )

        candidates = detect_temporal_anomalies(
            disease_id=stable_disease.id,
            lookback_days=14,
            baseline_window_days=4,
            region_type="home",
        )

        self.assertEqual(candidates, [])

    def test_admin_can_detect_anomalies_from_api(self):
        for index, current_date in enumerate([date(2026, 4, 5), date(2026, 4, 6), date(2026, 4, 7), date(2026, 4, 8)], start=1):
            patient = self._create_extra_patient(index + 40)
            self._create_case(
                patient=patient,
                doctor=self.doctor,
                disease=self.disease_b,
                diagnosis_date=current_date,
                latitude=33.5400 + (index * 0.0002),
                longitude=36.2800 + (index * 0.0002),
            )

        for index in range(45, 49):
            patient = self._create_extra_patient(index)
            self._create_case(
                patient=patient,
                doctor=self.second_doctor,
                disease=self.disease_b,
                diagnosis_date=date(2026, 4, 9),
                latitude=33.5410 + (index * 0.0001),
                longitude=36.2810 + (index * 0.0001),
            )

        self.client.force_authenticate(user=self.admin_user)
        response = self.client.get(
            reverse("report-detect-anomalies"),
            {
                "disease": self.disease_b.id,
                "lookback_days": 14,
                "baseline_window_days": 4,
                "region_type": "home",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payload = response.json()
        self.assertEqual(payload["candidate_count"], 1)
        self.assertEqual(payload["candidates"][0]["disease_id"], self.disease_b.id)
        self.assertEqual(payload["candidates"][0]["target_date"], "2026-04-09")

class SupervisedRiskPredictionTests(CoreAPITestCase):
    def _create_labeled_visit(
        self,
        *,
        patient,
        doctor,
        disease,
        diagnosis_date,
        latitude,
        longitude,
        alert_level,
        risk_score,
    ):
        visit = Visit.objects.create(
            patient=patient,
            doctor=doctor,
            disease=disease,
            diagnosis_date=diagnosis_date,
            status="infected",
            weight=70,
            height=175,
            marital_status="single",
        )
        GeoData.objects.create(
            patient=patient,
            visit=visit,
            latitude=latitude,
            longitude=longitude,
            region_type="home",
        )
        Report.objects.create(
            disease=disease,
            trigger_visit=visit,
            analysis_period_start=diagnosis_date,
            analysis_period_end=diagnosis_date,
            alert_level=alert_level,
            summary=f"{alert_level} alert",
            risk_score=risk_score,
            nearby_case_count=1 if alert_level != "low" else 0,
            current_case_count=1,
            previous_case_count=0,
            growth_rate=1.0 if alert_level in {"high", "critical"} else 0.0,
            surge_ratio=2.0 if alert_level in {"high", "critical"} else 1.0,
            reasons=[alert_level],
            status="new",
        )
        return visit

    def test_train_baseline_risk_model_creates_artifact_and_samples(self):
        critical_disease = Disease.objects.create(
            disease_code="CRIT",
            name="Critical Disease",
            type="viral",
            transmission_vector="airborne",
            symptoms="fever",
            risk_level=5,
            infection_score=0.95,
            policy_profile="high_priority",
            high_priority=True,
        )
        medium_disease = Disease.objects.create(
            disease_code="MED",
            name="Moderate Disease",
            type="bacterial",
            transmission_vector="waterborne",
            symptoms="pain",
            risk_level=3,
            infection_score=0.45,
            policy_profile="cluster_sensitive",
        )

        extra_patient_one = Patient.objects.create(
            national_number="7001",
            name="Risk Patient One",
            birth_date=date(1992, 1, 1),
            gender="male",
            residence_lat=33.5000,
            residence_long=36.2500,
            work_lat=33.5100,
            work_long=36.2600,
        )
        extra_patient_two = Patient.objects.create(
            national_number="7002",
            name="Risk Patient Two",
            birth_date=date(1993, 2, 2),
            gender="female",
            residence_lat=33.5200,
            residence_long=36.2700,
            work_lat=33.5300,
            work_long=36.2800,
        )

        low_visit = self._create_labeled_visit(
            patient=self.patient_one,
            doctor=self.doctor,
            disease=medium_disease,
            diagnosis_date=date(2026, 4, 10),
            latitude=33.5000,
            longitude=36.2500,
            alert_level="low",
            risk_score=10.0,
        )
        critical_visit = self._create_labeled_visit(
            patient=extra_patient_one,
            doctor=self.second_doctor,
            disease=critical_disease,
            diagnosis_date=date(2026, 4, 11),
            latitude=33.5005,
            longitude=36.2504,
            alert_level="critical",
            risk_score=95.0,
        )
        medium_visit = self._create_labeled_visit(
            patient=extra_patient_two,
            doctor=self.doctor,
            disease=medium_disease,
            diagnosis_date=date(2026, 4, 12),
            latitude=33.5400,
            longitude=36.2800,
            alert_level="medium",
            risk_score=35.0,
        )

        samples = build_training_samples()
        self.assertGreaterEqual(len(samples), 3)
        self.assertTrue(any(sample.label == "critical" for sample in samples))

        model_path = Path.cwd() / f"risk_model_test_{uuid4().hex}.json"
        try:
            result = train_baseline_risk_model(output_path=model_path)
            self.assertTrue(model_path.exists())
            artifact = result["artifact"]
            self.assertEqual(artifact["sample_count"], len(samples))
            self.assertIn("critical", artifact["label_counts"])

            loaded_model = load_risk_model(model_path=model_path)
            self.assertIn("class_centroids", loaded_model)

            prediction = predict_visit_risk(visit=critical_visit, model_path=model_path)
            self.assertEqual(prediction.visit_id, critical_visit.id)
            self.assertEqual(prediction.predicted_label, "critical")
            self.assertGreater(prediction.confidence, 0)

            endpoint_prediction = predict_visit_risk(visit=low_visit, model_path=model_path)
            self.assertIn(endpoint_prediction.predicted_label, {"low", "medium"})
            self.assertIn("critical", prediction.class_distances)
        finally:
            _unlink_with_retries(model_path)

    def test_train_and_predict_commands_and_api_endpoint(self):
        high_disease = Disease.objects.create(
            disease_code="HIGH",
            name="High Disease",
            type="viral",
            transmission_vector="airborne",
            symptoms="fever",
            risk_level=4,
            infection_score=0.85,
            policy_profile="high_priority",
            high_priority=True,
        )
        baseline_disease = Disease.objects.create(
            disease_code="BASE",
            name="Baseline Disease",
            type="viral",
            transmission_vector="airborne",
            symptoms="cough",
            risk_level=2,
            infection_score=0.35,
        )
        extra_patient = Patient.objects.create(
            national_number="7003",
            name="Risk Patient Three",
            birth_date=date(1994, 3, 3),
            gender="male",
            residence_lat=33.5100,
            residence_long=36.2600,
            work_lat=33.5200,
            work_long=36.2700,
        )
        self._create_labeled_visit(
            patient=self.patient_one,
            doctor=self.doctor,
            disease=baseline_disease,
            diagnosis_date=date(2026, 4, 10),
            latitude=33.5000,
            longitude=36.2500,
            alert_level="low",
            risk_score=8.0,
        )
        target_visit = self._create_labeled_visit(
            patient=extra_patient,
            doctor=self.second_doctor,
            disease=high_disease,
            diagnosis_date=date(2026, 4, 11),
            latitude=33.5004,
            longitude=36.2503,
            alert_level="high",
            risk_score=70.0,
        )
        self._create_labeled_visit(
            patient=self.patient_two,
            doctor=self.second_doctor,
            disease=high_disease,
            diagnosis_date=date(2026, 4, 12),
            latitude=33.5008,
            longitude=36.2506,
            alert_level="critical",
            risk_score=90.0,
        )

        model_path = Path.cwd() / f"risk_model_test_{uuid4().hex}.json"
        try:
            call_command("train_risk_model", output_path=str(model_path))
            self.assertTrue(model_path.exists())

            prediction = predict_visit_risk(visit=target_visit, model_path=model_path)
            self.assertIn(prediction.predicted_label, {"high", "critical"})

            self.client.force_authenticate(user=self.admin_user)
            response = self.client.get(
                reverse("visit-predict-risk", args=[target_visit.id]),
                {"model_path": str(model_path)},
            )

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            payload = response.json()
            self.assertEqual(payload["visit_id"], target_visit.id)
            self.assertIn(payload["predicted_label"], {"high", "critical"})
            self.assertIn("feature_values", payload)
        finally:
            _unlink_with_retries(model_path)

class RandomForestAlertPredictionTests(CoreAPITestCase):
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

    def _create_report_sample(self, *, disease, alert_level, risk_score, national_number):
        patient = Patient.objects.create(
            national_number=national_number,
            name=f"RF Patient {national_number}",
            birth_date=date(1992, 1, 1),
            gender="male",
            residence_lat=33.50,
            residence_long=36.25,
            work_lat=33.51,
            work_long=36.26,
        )
        visit = Visit.objects.create(
            patient=patient,
            doctor=self.doctor,
            disease=disease,
            diagnosis_date=date(2026, 4, 15),
            status="infected",
            weight=70,
            height=175,
            marital_status="single",
        )
        GeoData.objects.create(
            patient=patient,
            visit=visit,
            latitude=33.50,
            longitude=36.25,
            region_type="home",
        )
        Report.objects.create(
            disease=disease,
            trigger_visit=visit,
            analysis_period_start=date(2026, 4, 15),
            analysis_period_end=date(2026, 4, 15),
            alert_level=alert_level,
            summary=f"{alert_level} alert",
            risk_score=risk_score,
            nearby_case_count=1 if alert_level != "low" else 0,
            current_case_count=2 if alert_level != "low" else 1,
            previous_case_count=0,
            growth_rate=1.0 if alert_level in {"high", "critical"} else 0.0,
            surge_ratio=2.0 if alert_level in {"high", "critical"} else 1.0,
            reasons=[alert_level],
            status="new",
        )
        return visit

    def test_random_forest_training_creates_artifacts_and_prediction(self):
        low_disease = Disease.objects.create(
            disease_code="RFLOW",
            name="RF Low Disease",
            type="viral",
            transmission_vector="airborne",
            symptoms="fever",
            risk_level=1,
            infection_score=0.2,
        )
        critical_disease = Disease.objects.create(
            disease_code="RFCRIT",
            name="RF Critical Disease",
            type="viral",
            transmission_vector="airborne",
            symptoms="fever",
            risk_level=5,
            infection_score=0.95,
            high_priority=True,
        )
        self._create_report_sample(
            disease=low_disease,
            alert_level="low",
            risk_score=10.0,
            national_number="91001",
        )
        self._create_report_sample(
            disease=critical_disease,
            alert_level="critical",
            risk_score=92.0,
            national_number="91002",
        )

        model_path = Path.cwd() / f"rf_alert_model_test_{uuid4().hex}.joblib"
        metadata_path = model_path.with_suffix(".metadata.json")
        try:
            with patch(
                "core.services.ml.random_forest._load_ml_dependencies",
                return_value=_fake_ml_dependencies(),
            ):
                result = train_random_forest_model(
                    output_path=model_path,
                    metadata_path=metadata_path,
                    n_estimators=5,
                )
                prediction = predict_alert_level(
                    features={
                        "nearby_case_count": 3,
                        "current_case_count": 4,
                        "previous_case_count": 0,
                        "growth_rate": 2.0,
                        "surge_ratio": 4.0,
                        "rule_based_risk_score": 90.0,
                        "disease_risk_level": 5,
                        "infection_score": 0.95,
                        "high_priority": 1,
                        "rare_disease": 0,
                    },
                    model_path=model_path,
                    metadata_path=metadata_path,
                )

            self.assertTrue(model_path.exists())
            self.assertTrue(metadata_path.exists())
            self.assertEqual(result.metadata["sample_count"], 2)
            self.assertEqual(result.metadata["model_type"], "RandomForestClassifier")
            self.assertEqual(prediction.predicted_alert_level, "critical")
            self.assertGreater(prediction.confidence, 0)
            self.assertIn("critical", prediction.probabilities)
        finally:
            _unlink_with_retries(model_path)
            _unlink_with_retries(metadata_path)

    def test_train_random_forest_command_writes_model_and_metadata(self):
        self._create_report_sample(
            disease=self.disease_b,
            alert_level="low",
            risk_score=8.0,
            national_number="91003",
        )
        self._create_report_sample(
            disease=self.disease_a,
            alert_level="high",
            risk_score=70.0,
            national_number="91004",
        )
        model_path = Path.cwd() / f"rf_command_model_test_{uuid4().hex}.joblib"
        metadata_path = model_path.with_suffix(".metadata.json")
        try:
            with patch(
                "core.services.ml.random_forest._load_ml_dependencies",
                return_value=_fake_ml_dependencies(),
            ):
                call_command(
                    "train_random_forest",
                    output_path=str(model_path),
                    metadata_path=str(metadata_path),
                    n_estimators=3,
                )

            self.assertTrue(model_path.exists())
            self.assertTrue(metadata_path.exists())
        finally:
            _unlink_with_retries(model_path)
            _unlink_with_retries(metadata_path)

    def test_outbreak_engine_uses_rule_based_when_random_forest_model_missing(self):
        disease = Disease.objects.create(
            disease_code="RFMISS",
            name="RF Missing Disease",
            type="viral",
            transmission_vector="airborne",
            symptoms="fever",
            risk_level=1,
            infection_score=0.2,
        )
        visit = Visit.objects.create(
            patient=self.patient_one,
            doctor=self.doctor,
            disease=disease,
            diagnosis_date=date(2026, 4, 16),
            status="infected",
            weight=70,
            height=175,
            marital_status="single",
        )
        geodata = GeoData.objects.create(
            patient=self.patient_one,
            visit=visit,
            latitude=33.50,
            longitude=36.25,
            region_type="home",
        )
        policy = get_policy_for_disease(
            disease_code=disease.disease_code,
            risk_level=disease.risk_level,
            infection_score=disease.infection_score,
        )

        with patch(
            "core.services.outbreak_engine.predict_random_forest_alert_level",
            side_effect=FileNotFoundError("model missing"),
        ):
            analysis = evaluate_visit_outbreak(
                context=self._build_context(visit, geodata),
                policy=policy,
            )

        self.assertEqual(analysis.alert_level, "low")
        self.assertEqual(
            analysis.metadata["ml"]["final_decision_source"],
            "rule_based_ml_unavailable",
        )
        self.assertEqual(analysis.metadata["ml"]["rule_based_alert_level"], "low")

    def test_low_confidence_random_forest_prediction_does_not_override_rule_based_alert(self):
        disease = Disease.objects.create(
            disease_code="RFLOWCONF",
            name="RF Low Confidence Disease",
            type="viral",
            transmission_vector="airborne",
            symptoms="fever",
            risk_level=1,
            infection_score=0.2,
        )
        visit = Visit.objects.create(
            patient=self.patient_two,
            doctor=self.second_doctor,
            disease=disease,
            diagnosis_date=date(2026, 4, 17),
            status="infected",
            weight=60,
            height=165,
            marital_status="single",
        )
        geodata = GeoData.objects.create(
            patient=self.patient_two,
            visit=visit,
            latitude=33.53,
            longitude=36.27,
            region_type="home",
        )
        policy = get_policy_for_disease(
            disease_code=disease.disease_code,
            risk_level=disease.risk_level,
            infection_score=disease.infection_score,
        )
        low_confidence_prediction = SimpleNamespace(
            predicted_alert_level="critical",
            confidence=0.2,
            probabilities={"low": 0.2, "medium": 0.2, "high": 0.2, "critical": 0.4},
            used_features={},
        )

        with patch(
            "core.services.outbreak_engine.predict_random_forest_alert_level",
            return_value=low_confidence_prediction,
        ):
            analysis = evaluate_visit_outbreak(
                context=self._build_context(visit, geodata),
                policy=policy,
            )

        self.assertEqual(analysis.alert_level, "low")
        self.assertEqual(analysis.metadata["ml"]["ml_alert_level"], "critical")
        self.assertEqual(
            analysis.metadata["ml"]["final_decision_source"],
            "rule_based_ml_low_confidence",
        )
