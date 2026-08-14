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

class AnalyticsFilteringTests(CoreAPITestCase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.doctor_user)

    def test_cases_over_time_respects_visit_filters(self):
        response = self.client.get(
            reverse("visit-cases-over-time"),
            {"disease": self.disease_a.id},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), [{"diagnosis_date": "2026-04-01", "total_cases": 2}])

    def test_cases_by_doctor_respects_visit_filters(self):
        response = self.client.get(
            reverse("visit-cases-by-doctor"),
            {"disease": self.disease_a.id},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.json(),
            [
                {"doctor__id": self.doctor.id, "doctor__user__username": "doctor1", "total_cases": 1},
                {"doctor__id": self.second_doctor.id, "doctor__user__username": "doctor2", "total_cases": 1},
            ],
        )

    def test_cases_by_region_type_respects_filtered_geodata(self):
        response = self.client.get(
            reverse("visit-cases-by-region-type"),
            {"disease": self.disease_a.id, "region_type": "home"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), [{"region_type": "home", "total_cases": 1}])

    def test_disease_region_matrix_respects_filtered_geodata(self):
        response = self.client.get(
            reverse("visit-disease-region-matrix"),
            {"disease": self.disease_a.id, "region_type": "home"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.json(),
            [{"visit__disease__name": "Measles", "region_type": "home", "total_cases": 1}],
        )

class ActiveVisitStateTests(CoreAPITestCase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.doctor_user)
        self.active_disease = Disease.objects.create(
            disease_code="ACT-COL",
            name="Active Cholera",
            type="bacterial",
            transmission_vector="water",
            symptoms="diarrhea",
            risk_level=4,
            infection_score=3.0,
        )
        self.secondary_disease = Disease.objects.create(
            disease_code="ACT-FLU",
            name="Active Influenza",
            type="viral",
            transmission_vector="airborne",
            symptoms="fever",
            risk_level=2,
            infection_score=1.5,
        )

    def _create_patient(self, index: int, *, latitude: float = 33.7000, longitude: float = 36.7000):
        return Patient.objects.create(
            national_number=f"9{index:04d}",
            name=f"Active Patient {index}",
            birth_date=date(1990, 1, 1),
            gender="male" if index % 2 else "female",
            residence_lat=latitude,
            residence_long=longitude,
            work_lat=latitude + 0.001,
            work_long=longitude + 0.001,
        )

    def _create_visit(
        self,
        *,
        patient,
        disease,
        diagnosis_date,
        status_value="infected",
        latitude: float | None = None,
        longitude: float | None = None,
        region_type="home",
    ):
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
        if latitude is not None and longitude is not None:
            GeoData.objects.create(
                patient=patient,
                visit=visit,
                latitude=latitude,
                longitude=longitude,
                region_type=region_type,
            )
        return visit

    def test_active_visits_use_latest_status_per_patient_and_disease(self):
        patient_a = self._create_patient(1)
        patient_b = self._create_patient(2)
        patient_c = self._create_patient(3)

        patient_a_infected = self._create_visit(
            patient=patient_a,
            disease=self.active_disease,
            diagnosis_date=date(2026, 4, 6),
        )
        patient_a_cured = self._create_visit(
            patient=patient_a,
            disease=self.active_disease,
            diagnosis_date=date(2026, 4, 9),
            status_value="cured",
        )
        patient_b_old = self._create_visit(
            patient=patient_b,
            disease=self.active_disease,
            diagnosis_date=date(2026, 4, 7),
        )
        patient_b_latest = self._create_visit(
            patient=patient_b,
            disease=self.active_disease,
            diagnosis_date=date(2026, 4, 10),
        )
        patient_c_cholera = self._create_visit(
            patient=patient_c,
            disease=self.active_disease,
            diagnosis_date=date(2026, 4, 8),
        )
        patient_c_flu = self._create_visit(
            patient=patient_c,
            disease=self.secondary_disease,
            diagnosis_date=date(2026, 4, 9),
            status_value="cured",
        )

        window = Visit.objects.filter(
            diagnosis_date__gte=date(2026, 4, 1),
            diagnosis_date__lte=date(2026, 4, 12),
        )
        cholera_active_ids = set(
            active_visits_queryset(window.filter(disease=self.active_disease)).values_list("id", flat=True)
        )

        self.assertEqual(cholera_active_ids, {patient_b_latest.id, patient_c_cholera.id})
        self.assertNotIn(patient_a_infected.id, cholera_active_ids)
        self.assertNotIn(patient_a_cured.id, cholera_active_ids)
        self.assertNotIn(patient_b_old.id, cholera_active_ids)
        self.assertEqual(active_visits_queryset(window.filter(disease=self.secondary_disease)).count(), 0)
        self.assertEqual(active_visits_queryset(window.filter(patient=patient_c)).count(), 1)
        self.assertNotIn(patient_c_flu.id, cholera_active_ids)

    def test_active_geodata_view_hides_cured_and_uses_latest_infected_visit_once(self):
        cured_patient = self._create_patient(10)
        followup_patient = self._create_patient(11)

        cured_infected = self._create_visit(
            patient=cured_patient,
            disease=self.active_disease,
            diagnosis_date=date(2026, 4, 5),
            latitude=33.7000,
            longitude=36.7000,
        )
        cured_latest = self._create_visit(
            patient=cured_patient,
            disease=self.active_disease,
            diagnosis_date=date(2026, 4, 8),
            status_value="cured",
            latitude=33.7002,
            longitude=36.7002,
        )
        old_followup = self._create_visit(
            patient=followup_patient,
            disease=self.active_disease,
            diagnosis_date=date(2026, 4, 6),
            latitude=33.7010,
            longitude=36.7010,
        )
        latest_followup = self._create_visit(
            patient=followup_patient,
            disease=self.active_disease,
            diagnosis_date=date(2026, 4, 9),
            latitude=33.7012,
            longitude=36.7012,
        )

        response = self.client.get(
            reverse("geodata-active"),
            {
                "disease": self.active_disease.id,
                "date_from": "2026-04-01",
                "date_to": "2026-04-12",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        visit_ids = [item["visit"] for item in response.json()]
        self.assertEqual(visit_ids, [latest_followup.id])
        self.assertNotIn(cured_infected.id, visit_ids)
        self.assertNotIn(cured_latest.id, visit_ids)
        self.assertNotIn(old_followup.id, visit_ids)

    def test_active_geodata_view_defaults_to_all_exposure_points_and_skips_missing_geodata(self):
        home_only_patient = self._create_patient(70)
        work_only_patient = self._create_patient(71)
        both_patient = self._create_patient(72)
        no_geodata_patient = self._create_patient(73)

        home_only_visit = self._create_visit(
            patient=home_only_patient,
            disease=self.active_disease,
            diagnosis_date=date(2026, 4, 13),
            latitude=33.7600,
            longitude=36.7600,
            region_type="home",
        )
        work_only_visit = self._create_visit(
            patient=work_only_patient,
            disease=self.active_disease,
            diagnosis_date=date(2026, 4, 13),
            latitude=33.7610,
            longitude=36.7610,
            region_type="work",
        )
        both_visit = self._create_visit(
            patient=both_patient,
            disease=self.active_disease,
            diagnosis_date=date(2026, 4, 13),
            latitude=33.7620,
            longitude=36.7620,
            region_type="home",
        )
        both_work_geodata = GeoData.objects.create(
            patient=both_patient,
            visit=both_visit,
            latitude=33.7630,
            longitude=36.7630,
            region_type="work",
        )
        no_geodata_visit = self._create_visit(
            patient=no_geodata_patient,
            disease=self.active_disease,
            diagnosis_date=date(2026, 4, 13),
        )

        response = self.client.get(
            reverse("geodata-active"),
            {
                "disease": self.active_disease.id,
                "date_from": "2026-04-13",
                "date_to": "2026-04-13",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payload = response.json()
        self.assertEqual(len(payload), 4)
        self.assertEqual(
            {(item["visit"], item["region_type"]) for item in payload},
            {
                (home_only_visit.id, "home"),
                (work_only_visit.id, "work"),
                (both_visit.id, "home"),
                (both_visit.id, "work"),
            },
        )
        self.assertIn(both_work_geodata.id, {item["id"] for item in payload})
        self.assertNotIn(no_geodata_visit.id, {item["visit"] for item in payload})

    def test_active_geodata_view_supports_case_mode_region_filters_and_invalid_point_mode(self):
        patient = self._create_patient(74)
        visit = self._create_visit(
            patient=patient,
            disease=self.active_disease,
            diagnosis_date=date(2026, 4, 14),
            latitude=33.7640,
            longitude=36.7640,
            region_type="home",
        )
        GeoData.objects.create(
            patient=patient,
            visit=visit,
            latitude=33.7650,
            longitude=36.7650,
            region_type="work",
        )

        base_params = {
            "disease": self.active_disease.id,
            "date_from": "2026-04-14",
            "date_to": "2026-04-14",
        }
        case_response = self.client.get(
            reverse("geodata-active"),
            {**base_params, "point_mode": "case"},
        )
        home_response = self.client.get(
            reverse("geodata-active"),
            {**base_params, "region_type": "home"},
        )
        work_response = self.client.get(
            reverse("geodata-active"),
            {**base_params, "region_type": "work"},
        )
        invalid_response = self.client.get(
            reverse("geodata-active"),
            {**base_params, "point_mode": "patient"},
        )

        self.assertEqual(case_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(case_response.json()), 1)
        self.assertEqual(case_response.json()[0]["visit"], visit.id)
        self.assertEqual(home_response.status_code, status.HTTP_200_OK)
        self.assertEqual([item["region_type"] for item in home_response.json()], ["home"])
        self.assertEqual(work_response.status_code, status.HTTP_200_OK)
        self.assertEqual([item["region_type"] for item in work_response.json()], ["work"])
        self.assertEqual(invalid_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("point_mode", invalid_response.json()["error"])

    def test_nearby_cases_ignore_cured_latest_and_deduplicate_followups(self):
        current_patient = self._create_patient(20)
        cured_patient = self._create_patient(21)
        followup_patient = self._create_patient(22)

        current_old = self._create_visit(
            patient=current_patient,
            disease=self.active_disease,
            diagnosis_date=date(2026, 4, 6),
            latitude=33.7098,
            longitude=36.7098,
        )
        current_visit = self._create_visit(
            patient=current_patient,
            disease=self.active_disease,
            diagnosis_date=date(2026, 4, 10),
            latitude=33.7100,
            longitude=36.7100,
        )
        current_geodata = GeoData.objects.get(visit=current_visit)
        cured_infected = self._create_visit(
            patient=cured_patient,
            disease=self.active_disease,
            diagnosis_date=date(2026, 4, 8),
            latitude=33.7103,
            longitude=36.7103,
        )
        cured_latest = self._create_visit(
            patient=cured_patient,
            disease=self.active_disease,
            diagnosis_date=date(2026, 4, 9),
            status_value="cured",
            latitude=33.7104,
            longitude=36.7104,
        )
        old_followup = self._create_visit(
            patient=followup_patient,
            disease=self.active_disease,
            diagnosis_date=date(2026, 4, 7),
            latitude=33.7105,
            longitude=36.7105,
        )
        latest_followup = self._create_visit(
            patient=followup_patient,
            disease=self.active_disease,
            diagnosis_date=date(2026, 4, 9),
            latitude=33.7106,
            longitude=36.7106,
        )

        context = VisitOutbreakContext(
            visit_id=current_visit.id,
            patient_id=current_visit.patient_id,
            doctor_id=current_visit.doctor_id,
            disease_id=current_visit.disease_id,
            disease_code=current_visit.disease.disease_code,
            diagnosis_date=current_visit.diagnosis_date,
            latitude=current_geodata.latitude,
            longitude=current_geodata.longitude,
            region_type=current_geodata.region_type,
        )
        nearby_cases = find_nearby_cases(context=context, radius_km=1.0, lookback_days=7)
        nearby_visit_ids = [case.visit_id for case in nearby_cases]

        self.assertEqual(nearby_visit_ids, [latest_followup.id])
        self.assertNotIn(cured_infected.id, nearby_visit_ids)
        self.assertNotIn(cured_latest.id, nearby_visit_ids)
        self.assertNotIn(current_old.id, nearby_visit_ids)
        self.assertNotIn(old_followup.id, nearby_visit_ids)

    def test_dbscan_uses_active_latest_patient_disease_visits_only(self):
        followup_patient = self._create_patient(30)
        active_patient = self._create_patient(31)
        cured_patient = self._create_patient(32)

        old_followup = self._create_visit(
            patient=followup_patient,
            disease=self.active_disease,
            diagnosis_date=date(2026, 4, 10),
            latitude=33.7200,
            longitude=36.7200,
        )
        latest_followup = self._create_visit(
            patient=followup_patient,
            disease=self.active_disease,
            diagnosis_date=date(2026, 4, 11),
            latitude=33.7202,
            longitude=36.7202,
        )
        active_visit = self._create_visit(
            patient=active_patient,
            disease=self.active_disease,
            diagnosis_date=date(2026, 4, 11),
            latitude=33.7204,
            longitude=36.7204,
        )
        cured_infected = self._create_visit(
            patient=cured_patient,
            disease=self.active_disease,
            diagnosis_date=date(2026, 4, 10),
            latitude=33.7206,
            longitude=36.7206,
        )
        cured_latest = self._create_visit(
            patient=cured_patient,
            disease=self.active_disease,
            diagnosis_date=date(2026, 4, 12),
            status_value="cured",
            latitude=33.7207,
            longitude=36.7207,
        )

        clusters = detect_dbscan_clusters(
            disease_id=self.active_disease.id,
            date_from=date(2026, 4, 9),
            date_to=date(2026, 4, 12),
            eps_km=1.0,
            min_samples=2,
        )

        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0].point_count, 2)
        self.assertEqual(set(clusters[0].member_visit_ids), {latest_followup.id, active_visit.id})
        self.assertNotIn(old_followup.id, clusters[0].member_visit_ids)
        self.assertNotIn(cured_infected.id, clusters[0].member_visit_ids)
        self.assertNotIn(cured_latest.id, clusters[0].member_visit_ids)

    def test_anomaly_detection_does_not_inflate_repeated_followup_visits(self):
        for index, current_date in enumerate(
            [date(2026, 4, 5), date(2026, 4, 6), date(2026, 4, 7), date(2026, 4, 8)],
            start=40,
        ):
            patient = self._create_patient(index)
            self._create_visit(
                patient=patient,
                disease=self.active_disease,
                diagnosis_date=current_date,
                latitude=33.7300 + (index * 0.0001),
                longitude=36.7300 + (index * 0.0001),
            )

        followup_patient = self._create_patient(50)
        for offset in range(3):
            self._create_visit(
                patient=followup_patient,
                disease=self.active_disease,
                diagnosis_date=date(2026, 4, 9),
                latitude=33.7350 + (offset * 0.0001),
                longitude=36.7350 + (offset * 0.0001),
            )

        candidates = detect_temporal_anomalies(
            disease_id=self.active_disease.id,
            lookback_days=14,
            baseline_window_days=4,
            region_type="home",
        )

        self.assertEqual(candidates, [])

    def test_evaluate_outbreak_returns_no_alert_for_cured_latest_visit(self):
        patient = self._create_patient(60)
        self._create_visit(
            patient=patient,
            disease=self.active_disease,
            diagnosis_date=date(2026, 4, 8),
            latitude=33.7400,
            longitude=36.7400,
        )
        cured_visit = self._create_visit(
            patient=patient,
            disease=self.active_disease,
            diagnosis_date=date(2026, 4, 10),
            status_value="cured",
            latitude=33.7401,
            longitude=36.7401,
        )
        cured_geodata = GeoData.objects.get(visit=cured_visit)

        policy = get_policy_for_disease(
            disease_code=self.active_disease.disease_code,
            risk_level=self.active_disease.risk_level,
            infection_score=self.active_disease.infection_score,
        )
        analysis = evaluate_visit_outbreak(
            context=VisitOutbreakContext(
                visit_id=cured_visit.id,
                patient_id=cured_visit.patient_id,
                doctor_id=cured_visit.doctor_id,
                disease_id=cured_visit.disease_id,
                disease_code=cured_visit.disease.disease_code,
                diagnosis_date=cured_visit.diagnosis_date,
                latitude=cured_geodata.latitude,
                longitude=cured_geodata.longitude,
                region_type=cured_geodata.region_type,
            ),
            policy=policy,
        )

        self.assertEqual(analysis.alert_level, "no_alert")
        self.assertEqual(analysis.metadata["local_case_count"], 0)
        self.assertFalse(analysis.metadata["context_active"])
