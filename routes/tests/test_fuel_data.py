"""Tests for the CSV fuel-price data service."""

import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings

from routes.exceptions import FuelDataError
from routes.services.fuel_data import FuelDataService, get_fuel_data_service
from routes.tests.utils import (
    FakeJsonResponse,
    write_city_coordinates,
    write_station_csv,
)

CITY_TABLE = {
    "testville|KS": [40.10, -90.10],
    "othertown|OK": [35.50, -97.50],
    "dupcity|TX": [31.00, -97.00],
}


@override_settings(NOMINATIM_MIN_INTERVAL_SECONDS=0)
class FuelDataServiceTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.city_file = write_city_coordinates(
            Path(self.tmp.name) / "cities.json", CITY_TABLE
        )

    def tearDown(self):
        self.tmp.cleanup()

    def make_service(self, rows) -> FuelDataService:
        csv_file = write_station_csv(Path(self.tmp.name) / "fuel.csv", rows)
        return FuelDataService(csv_file, self.city_file)

    def test_loads_valid_rows_and_attaches_coordinates(self):
        service = self.make_service(
            [(1, "STOP ONE", "Testville", "KS", 3.09), (2, "STOP TWO", "Othertown", "OK", 2.99)]
        )
        stations = service.get_stations()
        self.assertEqual(len(stations), 2)
        first = stations[0]
        self.assertEqual(first.name, "STOP ONE")
        self.assertAlmostEqual(first.price_per_gallon, 3.09)
        self.assertAlmostEqual(first.latitude, 40.10)
        self.assertAlmostEqual(first.longitude, -90.10)

    def test_missing_required_column_raises(self):
        csv_file = write_station_csv(
            Path(self.tmp.name) / "bad.csv",
            [(1, "STOP ONE", "Testville", "KS", 3.09)],
        )
        # Rename the Retail Price column to simulate a schema change.
        content = Path(csv_file).read_text().replace(",Retail Price", "")
        Path(csv_file).write_text(content)
        service = FuelDataService(csv_file, self.city_file)
        with self.assertRaises(FuelDataError):
            service.get_stations()

    def test_non_us_states_are_skipped(self):
        service = self.make_service(
            [(1, "US STOP", "Testville", "KS", 3.09), (2, "CA STOP", "Toronto", "ON", 3.50)]
        )
        stations = service.get_stations()
        self.assertEqual(len(stations), 1)
        self.assertEqual(stations[0].state, "KS")

    def test_invalid_prices_are_skipped(self):
        service = self.make_service(
            [
                (1, "GOOD", "Testville", "KS", 3.09),
                (2, "BAD TEXT", "Testville", "KS", "not-a-price"),
                (3, "NEGATIVE", "Testville", "KS", -1.0),
                (4, "ZERO", "Testville", "KS", 0.0),
            ]
        )
        stations = service.get_stations()
        self.assertEqual([s.name for s in stations], ["GOOD"])

    def test_duplicate_ids_keep_first_occurrence(self):
        service = self.make_service(
            [
                (1, "FIRST", "Testville", "KS", 3.09),
                (1, "SECOND", "Testville", "KS", 4.50),
            ]
        )
        stations = service.get_stations()
        self.assertEqual(len(stations), 1)
        self.assertEqual(stations[0].price_per_gallon, 3.09)

    def test_same_location_with_different_id_is_deduplicated(self):
        service = self.make_service(
            [
                (1, "PILOT 1", "Testville", "KS", 3.09),
                (2, "PILOT 1", "Testville", "KS", 4.20),
            ]
        )
        stations = service.get_stations()
        self.assertEqual(len(stations), 1)

    def test_rows_without_resolvable_coordinates_are_skipped(self):
        # Unknown cities fall back to geocoding - mock it as "not found" so
        # the test stays hermetic and the row is skipped deterministically.
        with patch(
            "routes.services.geocoding._session.get", return_value=FakeJsonResponse([])
        ):
            service = self.make_service(
                [
                    (1, "KNOWN", "Testville", "KS", 3.09),
                    (2, "UNKNOWN CITY", "Nowhere", "KS", 3.19),
                ]
            )
            stations = service.get_stations()
        self.assertEqual([s.name for s in stations], ["KNOWN"])
        stats = service.get_stats()
        self.assertEqual(stats["rows_without_coordinates_skipped"], 1)

    def test_empty_dataset_after_cleaning_raises(self):
        service = self.make_service([(1, "CA STOP", "Vancouver", "BC", 3.50)])
        with self.assertRaises(FuelDataError):
            service.get_stations()

    def test_missing_csv_file_raises(self):
        service = FuelDataService("/tmp/does-not-exist.csv", self.city_file)
        with self.assertRaises(FuelDataError):
            service.get_stations()

    def test_stats_report_cleaning_counts(self):
        service = self.make_service(
            [
                (1, "US STOP", "Testville", "KS", 3.09),
                (1, "US DUPLICATE", "Testville", "KS", 3.60),
                (2, "CA STOP", "Toronto", "ON", 3.50),
            ]
        )
        service.get_stations()
        stats = service.get_stats()
        self.assertEqual(stats["csv_rows_read"], 3)
        self.assertEqual(stats["non_us_rows_skipped"], 1)
        self.assertEqual(stats["duplicate_rows_removed"], 1)
        self.assertEqual(stats["usable_stations_loaded"], 1)

    def test_singleton_is_cached_per_process(self):
        self.assertIs(get_fuel_data_service(), get_fuel_data_service())
