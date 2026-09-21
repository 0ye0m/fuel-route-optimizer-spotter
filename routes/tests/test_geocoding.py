"""Tests for the Nominatim geocoding service (HTTP fully mocked)."""

from unittest.mock import patch

from django.test import override_settings

import requests
from django.test import TestCase

from routes.exceptions import ExternalServiceError, LocationNotFoundError
from routes.models import GeocodingCache
from routes.services.geocoding import geocode_location
from routes.tests.utils import FakeJsonResponse, nominatim_result


def side_effect_for(results_by_query: dict):
    """Dispatch fake Nominatim responses on the queried location string."""

    def _get(url, params=None, headers=None, timeout=None):
        query = (params or {}).get("q", "").lower()
        for fragment, results in results_by_query.items():
            if fragment.lower() in query:
                return FakeJsonResponse(results)
        return FakeJsonResponse([])

    return _get


@override_settings(NOMINATIM_MIN_INTERVAL_SECONDS=0, LOCAL_CITY_COORDINATES_ENABLED=False)
class GeocodingServiceTests(TestCase):
    @patch("routes.services.geocoding._session.get")
    def test_geocodes_location_and_caches_it(self, mock_get):
        mock_get.side_effect = side_effect_for(
            {"chicago": [nominatim_result(41.8781, -87.6298, "Illinois")]}
        )

        location = geocode_location("Chicago, IL")

        self.assertAlmostEqual(location.latitude, 41.8781)
        self.assertAlmostEqual(location.longitude, -87.6298)
        self.assertEqual(location.state, "Illinois")
        self.assertEqual(mock_get.call_count, 1)
        self.assertTrue(GeocodingCache.objects.filter(query__icontains="chicago").exists())

        # Second call must be served from the cache (no second HTTP call).
        again = geocode_location("Chicago, IL")
        self.assertAlmostEqual(again.latitude, 41.8781)
        self.assertEqual(mock_get.call_count, 1)

    @patch("routes.services.geocoding._session.get")
    def test_unknown_location_raises_404_error(self, mock_get):
        mock_get.side_effect = side_effect_for({})
        with self.assertRaises(LocationNotFoundError):
            geocode_location("Xyzzyville, ZZ")

    @patch("routes.services.geocoding._session.get")
    def test_non_us_result_is_rejected(self, mock_get):
        mock_get.side_effect = side_effect_for(
            {"toronto": [nominatim_result(43.65, -79.38, "Ontario", country_code="ca")]}
        )
        with self.assertRaises(LocationNotFoundError) as ctx:
            geocode_location("Toronto, Canada")
        self.assertIn("not within the United States", str(ctx.exception))

    @patch("routes.services.geocoding._session.get")
    def test_timeout_maps_to_502(self, mock_get):
        mock_get.side_effect = requests.Timeout()
        with self.assertRaises(ExternalServiceError):
            geocode_location("Chicago, IL")

    @patch("routes.services.geocoding._session.get")
    def test_rate_limit_maps_to_502(self, mock_get):
        mock_get.side_effect = None
        mock_get.return_value = FakeJsonResponse([], status_code=429)
        with self.assertRaises(ExternalServiceError):
            geocode_location("Chicago, IL")

    @patch("routes.services.geocoding._session.get")
    def test_malformed_payload_maps_to_502(self, mock_get):
        # A "US" result without lat/lon fields is malformed, not non-US.
        mock_get.side_effect = side_effect_for(
            {"chicago": [{"address": {"country_code": "us"}, "display_name": "Broken"}]}
        )
        with self.assertRaises(ExternalServiceError):
            geocode_location("Chicago, IL")
