"""End-to-end API tests for POST /api/route/.

Nominatim, OSRM and the fuel CSV are all replaced with controlled fakes so
tests are deterministic, fast and never touch the network.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

import requests
from django.test import TestCase, override_settings

from routes.services.fuel_data import FuelDataService
from routes.tests.utils import (
    FakeJsonResponse,
    nominatim_result,
    osrm_payload,
    straight_line_length_miles,
    westward_line,
    write_city_coordinates,
    write_station_csv,
)

ROUTE_URL = "/api/route/"
LINE_LAT = 40.0

NY = (40.7128, -74.0060)
CHICAGO = (41.8781, -87.6298)


def nominatim_dispatch(results_by_query: dict):
    def _get(url, params=None, headers=None, timeout=None):
        query = (params or {}).get("q", "").lower()
        for fragment, results in results_by_query.items():
            if fragment.lower() in query:
                return FakeJsonResponse(results)
        return FakeJsonResponse([])

    return _get


@override_settings(
    NOMINATIM_MIN_INTERVAL_SECONDS=0,
    LOCAL_CITY_COORDINATES_ENABLED=False,
    ROUTE_CACHE_ENABLED=False,
)
class ApiTestCase(TestCase):
    """Base fixture wiring: fake geocoder + fake router + synthetic CSV."""

    LINE_LON_START = -95.0
    LINE_LON_END = -80.0

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.city_file = write_city_coordinates(
            Path(self.tmp.name) / "cities.json",
            {
                "midstop|ks": [LINE_LAT, -89.375],
                "cheapsop|ks": [LINE_LAT, -82.8125],
                "onroute|ks": [LINE_LAT, -87.5],
                "offroute|ne": [45.0, -99.0],
                "gapstop|ks": [LINE_LAT, -94.0],
            },
        )
        self.csv_file = str(Path(self.tmp.name) / "fuel.csv")

    def tearDown(self):
        self.tmp.cleanup()

    # -- helpers -------------------------------------------------------
    def station_line_position(self, lon: float) -> float:
        """Expected along-route mileage of a station on the test line."""
        total = straight_line_length_miles(
            westward_line(LINE_LAT, self.LINE_LON_START, self.LINE_LON_END)
        )
        fraction = (lon - self.LINE_LON_START) / (self.LINE_LON_END - self.LINE_LON_START)
        return fraction * total

    def install(
        self,
        csv_rows,
        route_coordinates=None,
        geocode_results=None,
        osrm_code="Ok",
    ):
        """Patch fuel data + geocoding + routing for a request.

        NOTE: geocoding and routing both call the same shared ``requests``
        module, so a single URL-dispatching side effect is used for both.
        """
        write_station_csv(self.csv_file, csv_rows)
        fuel_service = FuelDataService(self.csv_file, self.city_file)

        if geocode_results is None:
            geocode_results = {
                "new york": [nominatim_result(*NY, "New York")],
                "chicago": [nominatim_result(*CHICAGO, "Illinois")],
            }

        if route_coordinates is None:
            route_coordinates = westward_line(
                LINE_LAT, self.LINE_LON_START, self.LINE_LON_END
            )
        route_length = straight_line_length_miles(route_coordinates)
        osrm = FakeJsonResponse(
            osrm_payload(route_coordinates, distance_miles=route_length, code=osrm_code)
        )

        def dispatch(url, params=None, headers=None, timeout=None):
            low_url = url.lower()
            if "nominatim" in low_url or "/search" in low_url:
                query = (params or {}).get("q", "").lower()
                for fragment, results in (geocode_results or {}).items():
                    if fragment.lower() in query:
                        return FakeJsonResponse(results)
                return FakeJsonResponse([])
            if "osrm" in low_url or "/route/v1/" in low_url:
                return osrm
            raise AssertionError(f"Unexpected URL requested in test: {url}")

        patcher = patch.multiple(
            "routes.views",
            get_fuel_data_service=lambda: fuel_service,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        for target in (
            "routes.services.geocoding._session.get",
            "routes.services.routing._session.get",
        ):
            p = patch(target, side_effect=dispatch)
            p.start()
            self.addCleanup(p.stop)

    def post_route(self, body=None, raw=None):
        if raw is not None:
            return self.client.post(
                ROUTE_URL, data=raw, content_type="application/json"
            )
        return self.client.post(ROUTE_URL, data=body, content_type="application/json")


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class ShortTripTests(ApiTestCase):
    """Test 1 - valid short-distance trip (no fuel stop required)."""

    def test_short_trip_requires_no_fuel_stops(self):
        # 2-degree line (~106 miles); one station far off the corridor and
        # one right on it - neither may trigger a purchase.
        self.install(
            [
                (1, "ON ROUTE STOP", "Onroute", "KS", 3.00),
                (2, "FAR STOP", "Offroute", "NE", 2.00),
            ],
            route_coordinates=westward_line(LINE_LAT, -88.0, -86.0),
        )
        response = self.post_route({"start": "New York, NY", "finish": "Chicago, IL"})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["fuel_stops"], [])
        self.assertEqual(data["fuel_summary"]["stops_required"], 0)
        self.assertEqual(data["fuel_summary"]["total_fuel_purchased_gallons"], 0.0)
        self.assertEqual(data["fuel_summary"]["total_fuel_cost"], 0.0)

        consumed = data["fuel_summary"]["total_fuel_consumed_gallons"]
        self.assertGreater(consumed, 0.0)
        self.assertAlmostEqual(
            consumed,
            round(data["fuel_summary"]["total_distance_miles"] / 10.0, 2),
            delta=0.6,
        )
        self.assertIn("full tank", data["fuel_summary"]["note"].lower())


class MultiStopTripTests(ApiTestCase):
    """Test 2 - trip requiring multiple fuel stops (route > 500 miles)."""

    def setUp(self):
        super().setUp()
        self.install(
            [
                (1, "MID STOP", "Midstop", "KS", 3.50),
                (2, "CHEAP STOP", "Cheapsop", "KS", 3.00),
            ]
        )
        self.response = self.post_route(
            {"start": "New York, NY", "finish": "Chicago, IL"}
        )
        self.data = self.response.json()

    def test_returns_two_stops_in_order(self):
        self.assertEqual(self.response.status_code, 200)
        stops = self.data["fuel_stops"]
        self.assertEqual(len(stops), 2)
        self.assertEqual([s["sequence"] for s in stops], [1, 2])
        self.assertAlmostEqual(stops[0]["price_per_gallon"], 3.5, places=2)
        self.assertAlmostEqual(stops[1]["price_per_gallon"], 3.0, places=2)

    def test_stop_positions_match_route_locations(self):
        stops = self.data["fuel_stops"]
        self.assertAlmostEqual(
            stops[0]["distance_from_start_miles"],
            self.station_line_position(-89.375),
            delta=1.0,
        )
        self.assertAlmostEqual(
            stops[1]["distance_from_start_miles"],
            self.station_line_position(-82.8125),
            delta=1.0,
        )

    def test_legs_respect_500_mile_range(self):
        """Test 8 - no driving segment may exceed the 500-mile tank range."""
        total = self.data["fuel_summary"]["total_distance_miles"]
        positions = [0.0] + [s["distance_from_start_miles"] for s in self.data["fuel_stops"]] + [total]
        legs = [b - a for a, b in zip(positions, positions[1:])]
        self.assertTrue(all(leg <= 500.0 + 1e-6 for leg in legs), legs)

    def test_gallons_follow_10_mpg(self):
        """Test 9 - fuel consumed / purchased follow the 10 MPG assumption."""
        summary = self.data["fuel_summary"]
        self.assertAlmostEqual(
            summary["total_fuel_consumed_gallons"],
            round(summary["total_distance_miles"] / 10.0, 2),
            delta=0.6,
        )
        purchased = sum(s["gallons_purchased"] for s in self.data["fuel_stops"])
        self.assertAlmostEqual(
            summary["total_fuel_purchased_gallons"], round(purchased, 2), places=2
        )
        for stop in self.data["fuel_stops"]:
            expected = round(
                stop["gallons_purchased"] * stop["price_per_gallon"], 2
            )
            self.assertAlmostEqual(stop["fuel_cost"], expected, places=2)

    def test_total_cost_is_sum_of_stop_costs(self):
        """Test 7 - fuel cost calculation."""
        expected_total = round(
            sum(s["fuel_cost"] for s in self.data["fuel_stops"]), 2
        )
        self.assertAlmostEqual(
            self.data["fuel_summary"]["total_fuel_cost"], expected_total, places=2
        )
        # ~15 gallons at each of $3.50 and $3.00 (greedy optimum for 793 mi)
        self.assertAlmostEqual(self.data["fuel_summary"]["total_fuel_cost"], 95.0, delta=2.0)

    def test_cheaper_station_deferral(self):
        """Test 10 - buys at $3.00 station what it can, deferring from $3.50."""
        stops = self.data["fuel_stops"]
        # First purchase only covers the gap to the cheaper station.
        self.assertAlmostEqual(stops[0]["gallons_purchased"], 14.5, delta=1.0)
        self.assertAlmostEqual(stops[1]["gallons_purchased"], 14.9, delta=1.0)

    def test_geometry_and_markers_present(self):
        route = self.data["route"]
        self.assertEqual(route["geometry"]["type"], "LineString")
        self.assertGreaterEqual(len(route["geometry"]["coordinates"]), 2)
        first = route["geometry"]["coordinates"][0]
        self.assertAlmostEqual(first[1], LINE_LAT, places=1)
        self.assertAlmostEqual(self.data["trip"]["start"]["latitude"], NY[0], places=3)
        self.assertAlmostEqual(self.data["trip"]["finish"]["latitude"], CHICAGO[0], places=3)

    def test_metadata_reports_corridor_matching(self):
        metadata = self.data["metadata"]
        self.assertEqual(metadata["fuel_stations_in_dataset"], 2)
        self.assertEqual(metadata["stations_within_corridor"], 2)


# ---------------------------------------------------------------------------
# Validation and error handling
# ---------------------------------------------------------------------------


class ValidationTests(ApiTestCase):
    """Tests 3-5 - request validation."""

    def test_identical_start_and_finish_rejected(self):
        self.install([])
        response = self.post_route({"start": "New York, NY", "finish": "new york, ny"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())

    def test_geocoded_same_location_rejected(self):
        self.install(
            [],
            geocode_results={
                "manhattan": [nominatim_result(40.7831, -73.9712, "New York")],
                "nyc": [nominatim_result(40.7831, -73.9712, "New York")],
            },
        )
        response = self.post_route({"start": "Manhattan", "finish": "NYC"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("same location", response.json()["error"].lower())

    def test_missing_fields_rejected(self):
        self.install([])
        response = self.post_route({})
        self.assertEqual(response.status_code, 400)
        self.assertIn("start", response.json()["error"].lower())

    def test_empty_strings_rejected(self):
        self.install([])
        response = self.post_route({"start": "   ", "finish": "Chicago, IL"})
        self.assertEqual(response.status_code, 400)

    def test_invalid_json_rejected(self):
        self.install([])
        response = self.post_route(raw="{not valid json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())


class ExternalFailureTests(ApiTestCase):
    """Tests 4-5 and upstream failures."""

    def test_invalid_location_returns_404(self):
        self.install([], geocode_results={})
        response = self.post_route({"start": "Nowhereville, ZZ", "finish": "Chicago, IL"})
        self.assertEqual(response.status_code, 404)
        self.assertIn("geocode", response.json()["error"].lower())

    def test_non_us_location_returns_404(self):
        self.install(
            [],
            geocode_results={
                "new york": [nominatim_result(*NY, "New York")],
                "toronto": [nominatim_result(43.65, -79.38, "Ontario", country_code="ca")],
            },
        )
        response = self.post_route({"start": "New York, NY", "finish": "Toronto, Canada"})
        self.assertEqual(response.status_code, 404)
        self.assertIn("united states", response.json()["error"].lower())

    def test_geocoding_timeout_returns_502(self):
        # Install a normal route but a geocoder that times out.
        self.install([])
        p = patch(
            "routes.services.geocoding._session.get", side_effect=requests.Timeout()
        )
        p.start()
        self.addCleanup(p.stop)
        response = self.post_route({"start": "New York, NY", "finish": "Chicago, IL"})
        self.assertEqual(response.status_code, 502)

    def test_routing_failure_returns_502(self):
        self.install([], osrm_code="NoRoute")
        response = self.post_route({"start": "New York, NY", "finish": "Chicago, IL"})
        self.assertEqual(response.status_code, 502)
        self.assertIn("route", response.json()["error"].lower())


class CoverageFailureTests(ApiTestCase):
    """Test 6 - no suitable fuel station available."""

    def test_no_stations_in_corridor_returns_422(self):
        # 793-mile route but the only stations are ~345 miles off the route.
        self.install([(1, "FAR STOP", "Offroute", "NE", 3.00)])
        response = self.post_route({"start": "New York, NY", "finish": "Chicago, IL"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("no fuel station", response.json()["error"].lower())

    def test_station_gap_beyond_range_returns_422(self):
        # One early station, then a 700+ mile gap to the destination.
        self.install([(1, "GAP STOP", "Gapstop", "KS", 3.00)])
        response = self.post_route({"start": "New York, NY", "finish": "Chicago, IL"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("500 miles", response.json()["error"])


class EndpointInfoTests(ApiTestCase):
    def test_get_returns_api_description(self):
        response = self.client.get(ROUTE_URL)
        self.assertEqual(response.status_code, 200)
        self.assertIn("endpoint", response.json())
