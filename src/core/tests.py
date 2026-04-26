from datetime import date
from pathlib import Path
import csv
import shutil
import time
from tempfile import NamedTemporaryFile
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from .services import DiseaseAlertPolicy, OutbreakAnalysis, TrendSnapshot, VisitOutbreakContext
from .services.active_cases import active_visits_queryset
from .services.alert_policies import get_policy_for_disease
from .services.anomaly_detection import detect_temporal_anomalies
from .services.cluster_service import ALERT_LEVEL_TO_RISK_LEVEL, create_cluster_from_analysis
from .services.dbscan_hotspots import detect_dbscan_clusters, persist_dbscan_clusters
from .services.disease_reference import REFERENCE_SOURCE_NAME, classify_reference_disease
from .services.outbreak_engine import evaluate_visit_outbreak
from .services.report_service import create_report_from_analysis
from .services.risk_prediction import (
    build_training_samples,
    load_risk_model,
    predict_visit_risk,
    train_baseline_risk_model,
)
from .services.spatial import distance_km, find_nearby_cases
from .services.trend import build_trend_snapshot, count_cases_for_window
from .serializers import (
    DiseaseSerializer,
    GeoDataSerializer,
    MedicalHistorySerializer,
    PatientSerializer,
    ReportSerializer,
    UserSerializer,
    VisitSerializer,
)
from .models import (
    Allergy,
    Disease,
    Doctor,
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


class CoreAPITestCase(APITestCase):
    def setUp(self):
        self.admin_user = User.objects.create_user(
            username="admin",
            password="secret123",
            real_name="Admin User",
            phon_number="0001",
            role="admin",
        )
        self.doctor_user = User.objects.create_user(
            username="doctor1",
            password="secret123",
            real_name="Doctor One",
            phon_number="0002",
            role="doctor",
        )
        self.second_doctor_user = User.objects.create_user(
            username="doctor2",
            password="secret123",
            real_name="Doctor Two",
            phon_number="0003",
            role="doctor",
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


class PatientWorkflowTests(CoreAPITestCase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.doctor_user)

    def _visit_payload(self, **overrides):
        payload = {
            "doctor": self.doctor.id,
            "disease": self.disease_a.id,
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
        self.assertEqual(home_geodata.latitude, self.patient_one.residence_lat)
        self.assertEqual(home_geodata.longitude, self.patient_one.residence_long)
        self.assertEqual(work_geodata.latitude, self.patient_one.work_lat)
        self.assertEqual(work_geodata.longitude, self.patient_one.work_long)

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
        self.assertEqual(len(payload["clusters"]), 1)
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
