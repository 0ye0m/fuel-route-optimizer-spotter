"""Shared helpers for the test-suite: fake Nominatim/OSRM payloads and
synthetic route/station builders. No test ever hits the real external APIs."""

from __future__ import annotations

import csv
import json
from typing import List, Sequence
from unittest.mock import MagicMock

from routes.services.geo_utils import haversine_miles

METERS_PER_MILE = 1609.344

CSV_COLUMNS = [
    "OPIS Truckstop ID",
    "Truckstop Name",
    "Address",
    "City",
    "State",
    "Rack ID",
    "Retail Price",
]


class FakeJsonResponse:
    """Minimal stand-in for a ``requests.Response`` with a JSON body."""

    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code} raised on fake response")


def nominatim_result(lat: float, lon: float, state: str, country_code: str = "us") -> dict:
    return {
        "lat": str(lat),
        "lon": str(lon),
        "display_name": f"Somewhere, {state}, United States",
        "address": {"state": state, "country_code": country_code},
    }


def osrm_payload(
    coordinates: Sequence[Sequence[float]],
    distance_miles: float | None = None,
    duration_seconds: float = 36000.0,
    code: str = "Ok",
) -> dict:
    if distance_miles is None:
        distance_miles = straight_line_length_miles(coordinates)
    return {
        "code": code,
        "routes": [
            {
                "distance": distance_miles * METERS_PER_MILE,
                "duration": duration_seconds,
                "geometry": {"type": "LineString", "coordinates": list(coordinates)},
                "legs": [{"steps": []}],
            }
        ],
        "waypoints": [
            {"location": list(coordinates[0])},
            {"location": list(coordinates[-1])},
        ],
    }


def straight_line_length_miles(coordinates: Sequence[Sequence[float]]) -> float:
    total = 0.0
    for (lon1, lat1), (lon2, lat2) in zip(coordinates, coordinates[1:]):
        total += float(haversine_miles(lat1, lon1, lat2, lon2))
    return total


def westward_line(lat: float, lon_start: float, lon_end: float) -> List[List[float]]:
    """Two-point GeoJSON line along a constant latitude (uniform segments)."""
    return [[lon_start, lat], [lon_end, lat]]


def write_station_csv(
    path,
    rows: Sequence[Sequence[object]],
) -> str:
    """Write a fuel-price CSV in the exact format of the provided dataset.

    Each row: (id, name, city, state, price). Address/Rack ID are fixed.
    """
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for station_id, name, city, state, price in rows:
            writer.writerow([station_id, name, "1 Highway Ave", city, state, 100, price])
    return str(path)


def write_city_coordinates(path, mapping) -> str:
    """Write a bundled city-coordinate table (key 'city|STATE' -> [lat, lon])."""
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(mapping, handle)
    return str(path)


def mock_get_side_effect(routes_by_host: dict):
    """Build a ``requests.get`` side_effect that dispatches on URL contents."""
    def side_effect(url, params=None, headers=None, timeout=None):
        for fragment, response in routes_by_host.items():
            if fragment in url:
                return response
        raise AssertionError(f"Unexpected URL requested in test: {url}")

    return side_effect
