"""Fuel-price data service.

Loads the provided OPIS truck-stop CSV, validates and cleans it, filters it
down to United States stations, attaches coordinates to every station and
keeps the parsed result in an in-process cache so the CSV is read from disk
only once per server process.

The CSV does NOT contain latitude/longitude columns - it identifies stations
by City + State. Station coordinates are resolved from a bundled US city
coordinate table (``data/city_coordinates.json``, built from a public US
cities dataset and Nominatim). Any city that is not in the bundled table is
geocoded once through the geocoding service (and cached in the database), so
a future, updated CSV keeps working without code changes.
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from django.conf import settings

from routes.exceptions import FuelDataError

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = {
    "OPIS Truckstop ID",
    "Truckstop Name",
    "Address",
    "City",
    "State",
    "Retail Price",
}


@dataclass(frozen=True)
class FuelStation:
    """One usable fuel station from the CSV."""

    station_id: str
    name: str
    address: str
    city: str
    state: str
    price_per_gallon: float
    latitude: float
    longitude: float

    def to_dict(self) -> dict:
        return {
            "station_id": self.station_id,
            "station_name": self.name,
            "address": self.address,
            "city": self.city,
            "state": self.state,
            "price_per_gallon": self.price_per_gallon,
            "latitude": self.latitude,
            "longitude": self.longitude,
        }


@dataclass
class LoadStats:
    """Counters explaining what the loader kept / discarded (for debugging
    and for the API's metadata block)."""

    rows_read: int = 0
    invalid_price_rows: int = 0
    non_us_rows: int = 0
    duplicates_removed: int = 0
    rows_without_coordinates: int = 0
    stations_loaded: int = 0

    def to_dict(self) -> dict:
        return {
            "csv_rows_read": self.rows_read,
            "rows_with_invalid_price_skipped": self.invalid_price_rows,
            "non_us_rows_skipped": self.non_us_rows,
            "duplicate_rows_removed": self.duplicates_removed,
            "rows_without_coordinates_skipped": self.rows_without_coordinates,
            "usable_stations_loaded": self.stations_loaded,
        }


class FuelDataService:
    """Loads, validates, cleans and caches the fuel-price dataset."""

    def __init__(self, csv_path: str | None = None, city_coords_path: str | None = None):
        self.csv_path = Path(csv_path or settings.FUEL_DATA_PATH)
        self.city_coords_path = Path(city_coords_path or settings.CITY_COORDINATES_PATH)
        self._stations: list[FuelStation] | None = None
        self._stats = LoadStats()
        self._city_coords: dict[str, tuple[float, float]] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def load_data(self) -> list[FuelStation]:
        """Load and prepare the dataset exactly once per process."""
        if self._stations is not None:
            return self._stations

        if not self.csv_path.exists():
            raise FuelDataError(f"Fuel price data file not found: {self.csv_path.name}")

        raw_rows = self._read_csv()
        self.validate_columns(raw_rows)
        self._city_coords = self._load_city_coordinates()

        stations: list[FuelStation] = []
        seen_ids: set[str] = set()
        seen_locations: set[tuple] = set()

        for row in raw_rows:
            self._stats.rows_read += 1

            city = (row.get("City") or "").strip()
            state = (row.get("State") or "").strip().upper()
            name = (row.get("Truckstop Name") or "").strip()
            address = (row.get("Address") or "").strip()
            station_id = (row.get("OPIS Truckstop ID") or "").strip()

            price = self._parse_price(row.get("Retail Price"))
            if price is None:
                self._stats.invalid_price_rows += 1
                continue

            if state not in settings.US_STATE_CODES:
                # The raw file also contains Canadian provinces - the API
                # plans US routes, so non-US stations are not usable.
                self._stats.non_us_rows += 1
                continue

            # The file contains repeated records for the same truck stop
            # (same OPIS id, or same name+address with a different id).
            # Keep the first occurrence for deterministic behaviour.
            if station_id and station_id in seen_ids:
                self._stats.duplicates_removed += 1
                continue
            location_key = (name.lower(), address.lower(), city.lower(), state)
            if location_key in seen_locations:
                self._stats.duplicates_removed += 1
                continue

            coords = self._resolve_coordinates(city, state)
            if coords is None:
                self._stats.rows_without_coordinates += 1
                continue

            if station_id:
                seen_ids.add(station_id)
            seen_locations.add(location_key)

            stations.append(
                FuelStation(
                    station_id=station_id,
                    name=name,
                    address=address,
                    city=city,
                    state=state,
                    price_per_gallon=price,
                    latitude=coords[0],
                    longitude=coords[1],
                )
            )

        self._stats.stations_loaded = len(stations)
        if not stations:
            raise FuelDataError("The fuel price dataset is empty after cleaning.")

        self._stations = stations
        logger.info("Loaded %d usable fuel stations from %s", len(stations), self.csv_path.name)
        return self._stations

    def validate_columns(self, rows: list[dict]) -> None:
        """Ensure the CSV still has the columns the loader depends on."""
        if not rows:
            raise FuelDataError("The fuel price CSV contains no data rows.")
        available = set(rows[0].keys())
        missing = REQUIRED_COLUMNS - available
        if missing:
            raise FuelDataError(
                f"Fuel price CSV is missing required columns: {sorted(missing)}"
            )

    def get_stations(self) -> list[FuelStation]:
        return self.load_data()

    def get_stats(self) -> dict:
        self.load_data()  # ensure populated
        return self._stats.to_dict()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _read_csv(self) -> list[dict]:
        try:
            with open(self.csv_path, newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                return [
                    {(k or "").strip(): (v or "") for k, v in row.items()}
                    for row in reader
                ]
        except (OSError, csv.Error) as exc:
            raise FuelDataError(f"Fuel price CSV could not be read: {exc}") from exc

    @staticmethod
    def _parse_price(raw) -> float | None:
        try:
            price = float(str(raw).strip().replace("$", ""))
        except (TypeError, ValueError):
            return None
        if not price > 0.0 or price > 100.0:  # sanity bounds for a US gallon
            return None
        return price

    def _load_city_coordinates(self) -> dict[str, tuple[float, float]]:
        if not self.city_coords_path.exists():
            logger.warning(
                "City coordinate file %s not found; falling back to live geocoding "
                "for every station city.",
                self.city_coords_path.name,
            )
            return {}
        try:
            with open(self.city_coords_path, encoding="utf-8") as handle:
                raw = json.load(handle)
            return {k: (float(v[0]), float(v[1])) for k, v in raw.items()}
        except (OSError, ValueError, TypeError) as exc:
            raise FuelDataError(
                f"City coordinate file could not be loaded: {exc}"
            ) from exc

    def _resolve_coordinates(self, city: str, state: str) -> tuple[float, float] | None:
        """Coordinates for a station, from the bundled table, else a one-time
        geocode of 'City, STATE' (cached in the DB by the geocoding service)."""
        city_key = city.strip().lower()
        if self._city_coords:
            for key in (f"{city_key}|{state.upper()}", f"{city_key}|{state.lower()}"):
                coords = self._city_coords.get(key)
                if coords:
                    return coords

        # Fallback for future CSVs that include cities missing from the
        # bundled table: geocode once, then rely on the DB cache.
        from routes.services.geocoding import geocode_location

        try:
            location = geocode_location(f"{city.title()}, {state}")
        except Exception:
            logger.warning("Could not resolve coordinates for %s, %s", city, state)
            return None
        return (location.latitude, location.longitude)


@lru_cache(maxsize=None)
def get_fuel_data_service() -> FuelDataService:
    """Process-wide singleton (the CSV is parsed once, reused per request)."""
    return FuelDataService()
