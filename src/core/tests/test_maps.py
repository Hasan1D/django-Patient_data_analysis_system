from django.urls import reverse
from rest_framework import status

from .test_base import CoreAPITestCase


class MapCasesTests(CoreAPITestCase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.doctor_user)

    def _feature_ids(self, response):
        return {feature["properties"]["geodata_id"] for feature in response.json()["features"]}

    def test_active_cases_endpoint_returns_geojson_for_all_active_diseases(self):
        response = self.client.get(reverse("map-active-cases"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payload = response.json()
        self.assertEqual(payload["type"], "FeatureCollection")
        self.assertEqual(payload["count"], 3)
        self.assertEqual(len(payload["features"]), 3)
        first_feature = payload["features"][0]
        self.assertEqual(first_feature["type"], "Feature")
        self.assertEqual(first_feature["geometry"]["type"], "Point")
        self.assertIn("disease_code", first_feature["properties"])

    def test_active_cases_endpoint_filters_by_disease_code(self):
        response = self.client.get(
            reverse("map-active-cases"),
            {"disease_code": "mea"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payload = response.json()
        self.assertEqual(payload["count"], 2)
        disease_codes = {
            feature["properties"]["disease_code"]
            for feature in payload["features"]
        }
        self.assertEqual(disease_codes, {"MEA"})

    def test_active_cases_endpoint_filters_by_disease_type(self):
        response = self.client.get(
            reverse("map-active-cases"),
            {"disease_type": "viral"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payload = response.json()
        self.assertEqual(payload["count"], 2)
        disease_types = {
            feature["properties"]["disease_type"]
            for feature in payload["features"]
        }
        self.assertEqual(disease_types, {"viral"})

    def test_historical_cases_endpoint_filters_by_time_range(self):
        response = self.client.get(
            reverse("map-cases"),
            {
                "start_date": "2026-04-02",
                "end_date": "2026-04-02",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        feature = payload["features"][0]
        self.assertEqual(feature["properties"]["diagnosis_date"], "2026-04-02")
        self.assertEqual(feature["properties"]["disease_code"], "COL")

    def test_historical_cases_endpoint_rejects_invalid_date_range(self):
        response = self.client.get(
            reverse("map-cases"),
            {
                "start_date": "2026-04-03",
                "end_date": "2026-04-01",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("end_date", response.json())

    def test_map_endpoints_require_authentication(self):
        self.client.force_authenticate(user=None)

        response = self.client.get(reverse("map-active-cases"))

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
