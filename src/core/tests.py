from datetime import date
from tempfile import NamedTemporaryFile

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from .services import DiseaseAlertPolicy, OutbreakAnalysis, TrendSnapshot, VisitOutbreakContext
from .services.alert_policies import get_policy_for_disease
from .services.cluster_service import ALERT_LEVEL_TO_RISK_LEVEL, create_cluster_from_analysis
from .services.disease_reference import REFERENCE_SOURCE_NAME, classify_reference_disease
from .services.outbreak_engine import evaluate_visit_outbreak
from .services.report_service import create_report_from_analysis
from .services.spatial import distance_km, find_nearby_cases
from .services.trend import build_trend_snapshot, count_cases_for_window
from .serializers import DiseaseSerializer, ReportSerializer, UserSerializer
from .models import Disease, Doctor, GeoCluster, GeoData, Hospital, Patient, Report, Visit


User = get_user_model()


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
            status="confirmed",
            weight=70,
            height=175,
            marital_status="single",
        )
        self.visit_a2 = Visit.objects.create(
            patient=self.patient_two,
            doctor=self.second_doctor,
            disease=self.disease_a,
            diagnosis_date=date(2026, 4, 1),
            status="confirmed",
            weight=60,
            height=165,
            marital_status="married",
        )
        self.visit_b1 = Visit.objects.create(
            patient=self.patient_one,
            doctor=self.second_doctor,
            disease=self.disease_b,
            diagnosis_date=date(2026, 4, 2),
            status="confirmed",
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


class PermissionTests(CoreAPITestCase):
    def test_doctors_endpoint_requires_authentication(self):
        response = self.client.get(reverse("doctor-list"))

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_visits_endpoint_requires_doctor_or_admin(self):
        outsider = User.objects.create_user(
            username="visitor",
            password="secret123",
            real_name="Visitor User",
            phon_number="0004",
            role="guest",
        )
        self.client.force_authenticate(user=outsider)

        response = self.client.get(reverse("visit-list"))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_report_generate_is_admin_only(self):
        self.client.force_authenticate(user=self.doctor_user)

        response = self.client.get(
            reverse("report-generate"),
            {"disease": self.disease_a.id, "region_type": "home"},
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_can_generate_report_with_get_request(self):
        self.client.force_authenticate(user=self.admin_user)

        response = self.client.get(
            reverse("report-generate"),
            {"disease": self.disease_a.id, "region_type": "home"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["disease"], self.disease_a.id)
        self.assertEqual(response.json()["analysis_period_start"], "2026-04-01")
        self.assertEqual(response.json()["analysis_period_end"], "2026-04-01")
        self.assertEqual(response.json()["current_case_count"], 1)
        self.assertEqual(response.json()["alert_level"], "medium")

    def test_doctor_can_evaluate_outbreak_for_visit(self):
        self.client.force_authenticate(user=self.doctor_user)

        response = self.client.get(reverse("visit-evaluate-outbreak", args=[self.visit_a1.id]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["visit_id"], self.visit_a1.id)
        self.assertEqual(response.json()["disease"]["id"], self.disease_a.id)
        self.assertIn("analysis", response.json())
        self.assertIn("policy", response.json())


class SerializerTests(CoreAPITestCase):
    def test_user_serializer_excludes_password_field(self):
        serializer = UserSerializer(self.doctor_user)

        self.assertNotIn("password", serializer.data)
        self.assertEqual(serializer.data["username"], self.doctor_user.username)

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
            status="confirmed",
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
            status="confirmed",
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
            status="confirmed",
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
            status="confirmed",
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
            status="confirmed",
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
            status="confirmed",
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
            status="confirmed",
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
            status="confirmed",
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
            status="confirmed",
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


class DiseaseReferenceClassificationTests(APITestCase):
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
            status="confirmed",
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
            status="confirmed",
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
            status="confirmed",
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
            status="confirmed",
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
            status="confirmed",
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
            status="confirmed",
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
            status="confirmed",
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
            status="confirmed",
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
            status="confirmed",
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
            status="confirmed",
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
            status="confirmed",
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
            status="confirmed",
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
            status="confirmed",
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
            status="confirmed",
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


class MissingDataScenarioTests(CoreAPITestCase):
    def test_evaluate_outbreak_returns_clear_error_when_geodata_is_missing(self):
        visit_without_geodata = Visit.objects.create(
            patient=self.patient_one,
            doctor=self.doctor,
            disease=self.disease_a,
            diagnosis_date=date(2026, 4, 12),
            status="confirmed",
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
            status="confirmed",
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
            status="confirmed",
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
            status="confirmed",
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
            status="confirmed",
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
            status="confirmed",
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
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.json()[0]["alert_level"], "high")
        self.assertEqual(response.json()[0]["status"], "new")

    def test_active_alerts_can_be_filtered_by_disease(self):
        self.client.force_authenticate(user=self.admin_user)

        response = self.client.get(
            reverse("report-active-alerts"),
            {"disease": self.disease_b.id},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), [])

    def test_admin_can_view_active_hotspots_only(self):
        self.client.force_authenticate(user=self.admin_user)

        response = self.client.get(reverse("geocluster-active-hotspots"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.json()[0]["risk_level"], 4)
