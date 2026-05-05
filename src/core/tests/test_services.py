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

class ServicesArchitectureTests(APITestCase):
    def test_contract_dataclasses_hold_expected_values(self):
        context = VisitOutbreakContext(
            visit_id=1,
            disease_id=2,
            disease_code="MEA",
            diagnosis_date=date(2026, 4, 10),
            latitude=33.51,
            longitude=36.27,
            region_type="home",
        )
        trend = TrendSnapshot(
            current_count=5,
            previous_count=2,
            growth_rate=1.5,
            surge_ratio=2.5,
        )
        analysis = OutbreakAnalysis(
            alert_level="high",
            score=72.5,
            reasons=["cluster detected"],
            nearby_case_count=5,
            matched_case_ids=[1, 2, 3],
            trend=trend,
            should_create_report=True,
            should_create_cluster=True,
        )
        policy = DiseaseAlertPolicy(disease_code="MEA", lookback_days=7, radius_km=3.0)

        self.assertEqual(context.disease_code, "MEA")
        self.assertEqual(trend.current_count, 5)
        self.assertTrue(analysis.should_create_report)
        self.assertEqual(policy.radius_km, 3.0)

    def test_persistence_services_reject_non_persistable_analysis(self):
        context = VisitOutbreakContext(
            visit_id=1,
            disease_id=1,
            disease_code="COL",
            diagnosis_date=date(2026, 4, 10),
            latitude=33.5,
            longitude=36.3,
            region_type="home",
        )
        policy = DiseaseAlertPolicy(disease_code="COL")
        analysis = OutbreakAnalysis(alert_level="low", score=10.0)

        with self.assertRaises(ValueError):
            create_report_from_analysis(context=context, analysis=analysis)
        with self.assertRaises(ValueError):
            create_cluster_from_analysis(context=context, analysis=analysis)

class SpatialServiceTests(CoreAPITestCase):
    def setUp(self):
        super().setUp()
        self.current_visit = Visit.objects.create(
            patient=self.patient_one,
            doctor=self.doctor,
            disease=self.disease_a,
            diagnosis_date=date(2026, 4, 10),
            status="infected",
            weight=71,
            height=175,
            marital_status="single",
        )
        self.current_geodata = GeoData.objects.create(
            patient=self.patient_one,
            visit=self.current_visit,
            latitude=33.5000,
            longitude=36.2500,
            region_type="home",
        )

        self.patient_three = Patient.objects.create(
            national_number="1003",
            name="Patient Three",
            birth_date=date(1991, 3, 3),
            gender="male",
            residence_lat=33.5040,
            residence_long=36.2520,
            work_lat=33.5050,
            work_long=36.2530,
        )
        self.patient_four = Patient.objects.create(
            national_number="1004",
            name="Patient Four",
            birth_date=date(1993, 4, 4),
            gender="female",
            residence_lat=33.5060,
            residence_long=36.2510,
            work_lat=33.5070,
            work_long=36.2520,
        )
        self.patient_five = Patient.objects.create(
            national_number="1005",
            name="Patient Five",
            birth_date=date(1994, 5, 5),
            gender="male",
            residence_lat=33.6200,
            residence_long=36.4000,
            work_lat=33.6210,
            work_long=36.4010,
        )
        self.patient_six = Patient.objects.create(
            national_number="1006",
            name="Patient Six",
            birth_date=date(1995, 6, 6),
            gender="female",
            residence_lat=33.5005,
            residence_long=36.2505,
            work_lat=33.5010,
            work_long=36.2510,
        )

        self.nearby_home_visit = Visit.objects.create(
            patient=self.patient_three,
            doctor=self.second_doctor,
            disease=self.disease_a,
            diagnosis_date=date(2026, 4, 8),
            status="infected",
            weight=68,
            height=171,
            marital_status="single",
        )
        GeoData.objects.create(
            patient=self.patient_three,
            visit=self.nearby_home_visit,
            latitude=33.5040,
            longitude=36.2520,
            region_type="home",
        )

        self.nearby_work_visit = Visit.objects.create(
            patient=self.patient_four,
            doctor=self.second_doctor,
            disease=self.disease_a,
            diagnosis_date=date(2026, 4, 9),
            status="infected",
            weight=66,
            height=168,
            marital_status="married",
        )
        GeoData.objects.create(
            patient=self.patient_four,
            visit=self.nearby_work_visit,
            latitude=33.5060,
            longitude=36.2510,
            region_type="work",
        )

        self.far_visit = Visit.objects.create(
            patient=self.patient_five,
            doctor=self.second_doctor,
            disease=self.disease_a,
            diagnosis_date=date(2026, 4, 9),
            status="infected",
            weight=72,
            height=178,
            marital_status="single",
        )
        GeoData.objects.create(
            patient=self.patient_five,
            visit=self.far_visit,
            latitude=33.6200,
            longitude=36.4000,
            region_type="home",
        )

        self.old_visit = Visit.objects.create(
            patient=self.patient_six,
            doctor=self.doctor,
            disease=self.disease_a,
            diagnosis_date=date(2026, 3, 28),
            status="infected",
            weight=59,
            height=162,
            marital_status="single",
        )
        GeoData.objects.create(
            patient=self.patient_six,
            visit=self.old_visit,
            latitude=33.5005,
            longitude=36.2505,
            region_type="home",
        )

        self.other_disease_visit = Visit.objects.create(
            patient=self.patient_two,
            doctor=self.second_doctor,
            disease=self.disease_b,
            diagnosis_date=date(2026, 4, 9),
            status="infected",
            weight=61,
            height=165,
            marital_status="married",
        )
        GeoData.objects.create(
            patient=self.patient_two,
            visit=self.other_disease_visit,
            latitude=33.5008,
            longitude=36.2508,
            region_type="home",
        )

        self.context = VisitOutbreakContext(
            visit_id=self.current_visit.id,
            patient_id=self.patient_one.id,
            doctor_id=self.doctor.id,
            disease_id=self.disease_a.id,
            disease_code=self.disease_a.disease_code,
            diagnosis_date=self.current_visit.diagnosis_date,
            latitude=self.current_geodata.latitude,
            longitude=self.current_geodata.longitude,
            region_type=self.current_geodata.region_type,
        )

    def test_distance_km_returns_zero_for_same_point(self):
        self.assertEqual(distance_km(33.5, 36.3, 33.5, 36.3), 0.0)

    def test_distance_km_matches_expected_latitude_distance(self):
        self.assertAlmostEqual(distance_km(0.0, 0.0, 1.0, 0.0), 111.19, places=1)

    def test_find_nearby_cases_filters_by_disease_time_and_radius(self):
        nearby_cases = find_nearby_cases(
            context=self.context,
            radius_km=1.0,
            lookback_days=7,
        )

        self.assertEqual(
            [case.visit_id for case in nearby_cases],
            [self.nearby_home_visit.id, self.nearby_work_visit.id],
        )
        self.assertTrue(all(case.distance_km <= 1.0 for case in nearby_cases))

    def test_find_nearby_cases_can_filter_by_region_type(self):
        nearby_cases = find_nearby_cases(
            context=self.context,
            radius_km=1.0,
            lookback_days=7,
            region_type="home",
        )

        self.assertEqual(len(nearby_cases), 1)
        self.assertEqual(nearby_cases[0].visit_id, self.nearby_home_visit.id)
        self.assertEqual(nearby_cases[0].region_type, "home")

    def test_find_nearby_cases_excludes_current_visit(self):
        nearby_cases = find_nearby_cases(
            context=self.context,
            radius_km=1.0,
            lookback_days=7,
        )

        self.assertNotIn(self.current_visit.id, [case.visit_id for case in nearby_cases])

    def test_find_nearby_cases_rejects_negative_values(self):
        with self.assertRaises(ValueError):
            find_nearby_cases(
                context=self.context,
                radius_km=-1.0,
                lookback_days=7,
            )

        with self.assertRaises(ValueError):
            find_nearby_cases(
                context=self.context,
                radius_km=1.0,
                lookback_days=-1,
            )

class SpatialPointCollectionTests(CoreAPITestCase):
    def _create_patient(
        self,
        index: int,
        *,
        residence_lat: float | None = None,
        residence_long: float | None = None,
        work_lat: float | None = None,
        work_long: float | None = None,
    ) -> Patient:
        return Patient.objects.create(
            national_number=f"SP-{index:04d}",
            name=f"Spatial Point Patient {index}",
            birth_date=date(1990, 1, 1),
            gender="male" if index % 2 else "female",
            residence_lat=residence_lat,
            residence_long=residence_long,
            work_lat=work_lat,
            work_long=work_long,
        )

    def _create_visit(self, *, patient: Patient, diagnosis_date=date(2026, 4, 15)) -> Visit:
        return Visit.objects.create(
            patient=patient,
            doctor=self.doctor,
            disease=self.disease_a,
            diagnosis_date=diagnosis_date,
            status="infected",
            weight=70,
            height=175,
            marital_status="single",
        )

    def test_collect_active_spatial_points_exposure_mode_handles_home_work_both_and_missing(self):
        home_patient = self._create_patient(1, residence_lat=33.7000, residence_long=36.7000)
        work_patient = self._create_patient(2, work_lat=33.7010, work_long=36.7010)
        both_patient = self._create_patient(
            3,
            residence_lat=33.7020,
            residence_long=36.7020,
            work_lat=33.7030,
            work_long=36.7030,
        )
        no_geodata_patient = self._create_patient(4)

        home_visit = self._create_visit(patient=home_patient)
        work_visit = self._create_visit(patient=work_patient)
        both_visit = self._create_visit(patient=both_patient)
        no_geodata_visit = self._create_visit(patient=no_geodata_patient)
        GeoData.objects.create(
            patient=home_patient,
            visit=home_visit,
            latitude=33.7000,
            longitude=36.7000,
            region_type="home",
        )
        GeoData.objects.create(
            patient=work_patient,
            visit=work_visit,
            latitude=33.7010,
            longitude=36.7010,
            region_type="work",
        )
        GeoData.objects.create(
            patient=both_patient,
            visit=both_visit,
            latitude=33.7020,
            longitude=36.7020,
            region_type="home",
        )
        GeoData.objects.create(
            patient=both_patient,
            visit=both_visit,
            latitude=33.7030,
            longitude=36.7030,
            region_type="work",
        )

        points = collect_active_spatial_points(
            disease_id=self.disease_a.id,
            date_from=date(2026, 4, 15),
            date_to=date(2026, 4, 15),
        )

        self.assertEqual(len(points), 4)
        self.assertEqual(
            {(point.visit_id, point.region_type) for point in points},
            {
                (home_visit.id, "home"),
                (work_visit.id, "work"),
                (both_visit.id, "home"),
                (both_visit.id, "work"),
            },
        )
        self.assertNotIn(no_geodata_visit.id, {point.visit_id for point in points})

    def test_collect_active_spatial_points_supports_case_mode_region_filters_and_invalid_mode(self):
        patient = self._create_patient(
            5,
            residence_lat=33.7040,
            residence_long=36.7040,
            work_lat=33.7050,
            work_long=36.7050,
        )
        visit = self._create_visit(patient=patient, diagnosis_date=date(2026, 4, 16))
        GeoData.objects.create(
            patient=patient,
            visit=visit,
            latitude=33.7040,
            longitude=36.7040,
            region_type="home",
        )
        GeoData.objects.create(
            patient=patient,
            visit=visit,
            latitude=33.7050,
            longitude=36.7050,
            region_type="work",
        )

        case_points = collect_active_spatial_points(
            disease_id=self.disease_a.id,
            date_from=date(2026, 4, 16),
            date_to=date(2026, 4, 16),
            point_mode="case",
        )
        home_points = collect_active_spatial_points(
            disease_id=self.disease_a.id,
            date_from=date(2026, 4, 16),
            date_to=date(2026, 4, 16),
            region_type="home",
        )
        work_points = collect_active_spatial_points(
            disease_id=self.disease_a.id,
            date_from=date(2026, 4, 16),
            date_to=date(2026, 4, 16),
            region_type="work",
        )
        legacy_exposure_points = collect_active_spatial_points(
            disease_id=self.disease_a.id,
            date_from=date(2026, 4, 16),
            date_to=date(2026, 4, 16),
            one_per_visit=False,
        )

        self.assertEqual(len(case_points), 1)
        self.assertEqual(case_points[0].visit_id, visit.id)
        self.assertEqual([point.region_type for point in home_points], ["home"])
        self.assertEqual([point.region_type for point in work_points], ["work"])
        self.assertEqual(len(legacy_exposure_points), 2)
        with self.assertRaises(ValueError):
            collect_active_spatial_points(
                disease_id=self.disease_a.id,
                point_mode="patient",
            )

class TrendServiceTests(CoreAPITestCase):
    def setUp(self):
        super().setUp()
        self.patient_three = Patient.objects.create(
            national_number="2003",
            name="Trend Patient Three",
            birth_date=date(1988, 3, 3),
            gender="male",
            residence_lat=33.5010,
            residence_long=36.2510,
            work_lat=33.5020,
            work_long=36.2520,
        )
        self.patient_four = Patient.objects.create(
            national_number="2004",
            name="Trend Patient Four",
            birth_date=date(1989, 4, 4),
            gender="female",
            residence_lat=33.5030,
            residence_long=36.2530,
            work_lat=33.5040,
            work_long=36.2540,
        )
        self.patient_five = Patient.objects.create(
            national_number="2005",
            name="Trend Patient Five",
            birth_date=date(1990, 5, 5),
            gender="male",
            residence_lat=33.5050,
            residence_long=36.2550,
            work_lat=33.5060,
            work_long=36.2560,
        )

        self.current_visit_one = Visit.objects.create(
            patient=self.patient_three,
            doctor=self.doctor,
            disease=self.disease_a,
            diagnosis_date=date(2026, 4, 8),
            status="infected",
            weight=74,
            height=176,
            marital_status="single",
        )
        GeoData.objects.create(
            patient=self.patient_three,
            visit=self.current_visit_one,
            latitude=33.5010,
            longitude=36.2510,
            region_type="home",
        )

        self.current_visit_two = Visit.objects.create(
            patient=self.patient_four,
            doctor=self.second_doctor,
            disease=self.disease_a,
            diagnosis_date=date(2026, 4, 9),
            status="infected",
            weight=63,
            height=167,
            marital_status="married",
        )
        GeoData.objects.create(
            patient=self.patient_four,
            visit=self.current_visit_two,
            latitude=33.5030,
            longitude=36.2530,
            region_type="home",
        )
        GeoData.objects.create(
            patient=self.patient_four,
            visit=self.current_visit_two,
            latitude=33.5031,
            longitude=36.2531,
            region_type="home",
        )

        self.current_visit_three = Visit.objects.create(
            patient=self.patient_five,
            doctor=self.doctor,
            disease=self.disease_a,
            diagnosis_date=date(2026, 4, 10),
            status="infected",
            weight=69,
            height=170,
            marital_status="single",
        )
        GeoData.objects.create(
            patient=self.patient_five,
            visit=self.current_visit_three,
            latitude=33.5050,
            longitude=36.2550,
            region_type="work",
        )

    def test_count_cases_for_window_counts_distinct_visits(self):
        count = count_cases_for_window(
            disease_id=self.disease_a.id,
            start_date=date(2026, 4, 8),
            end_date=date(2026, 4, 10),
            region_type="home",
        )

        self.assertEqual(count, 2)

    def test_count_cases_for_window_rejects_invalid_window(self):
        with self.assertRaises(ValueError):
            count_cases_for_window(
                disease_id=self.disease_a.id,
                start_date=date(2026, 4, 10),
                end_date=date(2026, 4, 8),
            )

    def test_build_trend_snapshot_compares_current_and_previous_windows(self):
        trend = build_trend_snapshot(
            disease_id=self.disease_a.id,
            current_start=date(2026, 4, 8),
            current_end=date(2026, 4, 10),
            previous_start=date(2026, 4, 1),
            previous_end=date(2026, 4, 7),
        )

        self.assertEqual(trend.current_count, 3)
        self.assertEqual(trend.previous_count, 2)
        self.assertAlmostEqual(trend.growth_rate, 0.5, places=4)
        self.assertAlmostEqual(trend.surge_ratio, 1.5, places=4)

    def test_build_trend_snapshot_handles_missing_baseline(self):
        trend = build_trend_snapshot(
            disease_id=self.disease_b.id,
            current_start=date(2026, 4, 2),
            current_end=date(2026, 4, 3),
            previous_start=date(2026, 3, 25),
            previous_end=date(2026, 3, 31),
        )

        self.assertEqual(trend.current_count, 1)
        self.assertEqual(trend.previous_count, 0)
        self.assertEqual(trend.growth_rate, 1.0)
        self.assertEqual(trend.surge_ratio, 1.0)

    def test_build_trend_snapshot_rejects_invalid_windows(self):
        with self.assertRaises(ValueError):
            build_trend_snapshot(
                disease_id=self.disease_a.id,
                current_start=date(2026, 4, 10),
                current_end=date(2026, 4, 8),
                previous_start=date(2026, 4, 1),
                previous_end=date(2026, 4, 7),
            )

        with self.assertRaises(ValueError):
            build_trend_snapshot(
                disease_id=self.disease_a.id,
                current_start=date(2026, 4, 8),
                current_end=date(2026, 4, 10),
                previous_start=date(2026, 4, 7),
                previous_end=date(2026, 4, 1),
            )

class AlertPolicyServiceTests(APITestCase):
    def test_generic_policy_normalizes_code_and_derives_default_thresholds(self):
        policy = get_policy_for_disease(
            disease_code=" flu ",
            risk_level=2,
            infection_score=1.5,
        )

        self.assertEqual(policy.disease_code, "FLU")
        self.assertFalse(policy.high_priority)
        self.assertFalse(policy.rare_disease)
        self.assertEqual(policy.lookback_days, 7)
        self.assertEqual(policy.cluster_case_threshold, 3)
        self.assertEqual(policy.critical_case_threshold, 6)

    def test_measles_policy_gets_high_priority_sensitive_thresholds(self):
        policy = get_policy_for_disease(
            disease_code="mea",
            risk_level=5,
            infection_score=4.5,
        )

        self.assertTrue(policy.high_priority)
        self.assertFalse(policy.rare_disease)
        self.assertEqual(policy.lookback_days, 14)
        self.assertEqual(policy.baseline_window_days, 21)
        self.assertEqual(policy.cluster_case_threshold, 1)
        self.assertEqual(policy.critical_case_threshold, 2)
        self.assertEqual(policy.severity_weight, 2.2)

    def test_polio_policy_is_rare_and_immediately_sensitive(self):
        policy = get_policy_for_disease(
            disease_code="pol",
            risk_level=5,
            infection_score=4.0,
        )

        self.assertTrue(policy.high_priority)
        self.assertTrue(policy.rare_disease)
        self.assertEqual(policy.lookback_days, 30)
        self.assertEqual(policy.radius_km, 10.0)
        self.assertEqual(policy.cluster_case_threshold, 1)
        self.assertEqual(policy.critical_case_threshold, 1)
        self.assertEqual(policy.rarity_weight, 3.0)

    def test_cholera_policy_emphasizes_density_and_trend_over_rarity(self):
        policy = get_policy_for_disease(
            disease_code="col",
            risk_level=4,
            infection_score=3.0,
        )

        self.assertFalse(policy.rare_disease)
        self.assertFalse(policy.high_priority)
        self.assertEqual(policy.baseline_window_days, 14)
        self.assertEqual(policy.cluster_case_threshold, 2)
        self.assertEqual(policy.critical_case_threshold, 4)
        self.assertEqual(policy.density_weight, 1.8)
        self.assertEqual(policy.trend_weight, 1.8)

    def test_blank_disease_code_is_rejected(self):
        with self.assertRaises(ValueError):
            get_policy_for_disease(
                disease_code="   ",
                risk_level=3,
                infection_score=2.0,
            )

    def test_icd11_cholera_code_uses_cholera_operational_policy(self):
        policy = get_policy_for_disease(
            disease_code="1A00",
            risk_level=4,
            infection_score=0.82,
            policy_profile="cluster_sensitive",
        )

        self.assertEqual(policy.disease_code, "1A00")
        self.assertEqual(policy.baseline_window_days, 14)
        self.assertEqual(policy.cluster_case_threshold, 2)
        self.assertEqual(policy.critical_case_threshold, 4)
        self.assertFalse(policy.rare_disease)

    def test_reference_flags_can_force_rare_policy_for_imported_codes(self):
        policy = get_policy_for_disease(
            disease_code="1C81",
            risk_level=5,
            infection_score=0.2,
            high_priority=True,
            rare_disease=True,
            policy_profile="rare",
        )

        self.assertTrue(policy.high_priority)
        self.assertTrue(policy.rare_disease)
        self.assertEqual(policy.cluster_case_threshold, 1)
        self.assertEqual(policy.critical_case_threshold, 1)
        self.assertEqual(policy.rarity_weight, 3.0)

    def test_influenza_icd11_policy_prefers_surge_thresholds(self):
        policy = get_policy_for_disease(
            disease_code="1E32",
            risk_level=4,
            infection_score=0.67,
            policy_profile="surge_sensitive",
        )

        self.assertEqual(policy.lookback_days, 7)
        self.assertEqual(policy.baseline_window_days, 21)
        self.assertEqual(policy.cluster_case_threshold, 4)
        self.assertEqual(policy.critical_case_threshold, 7)

    def test_tuberculosis_icd11_policy_uses_longer_observation_window(self):
        policy = get_policy_for_disease(
            disease_code="1B1Z",
            risk_level=4,
            infection_score=0.79,
            high_priority=True,
            policy_profile="surge_sensitive",
        )

        self.assertTrue(policy.high_priority)
        self.assertEqual(policy.lookback_days, 30)
        self.assertEqual(policy.baseline_window_days, 60)
        self.assertEqual(policy.cluster_case_threshold, 2)
