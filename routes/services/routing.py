"""Routing service (OSRM - Open Source Routing Machine).

Calculates the driving route between two coordinates using OSRM's public
demo server (free, no API key). All external-HTTP logic lives here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List

import requests
from django.conf import settings

from routes.exceptions import ExternalServiceError, RoutingError

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = settings.EXTERNAL_API_TIMEOUT_SECONDS
METERS_PER_MILE = 1609.344

# Shared session: reuses connections to the routing server across requests.
_session = requests.Session()


@dataclass(frozen=True)
class RouteStep:
    """A condensed driving maneuver from OSRM's ``steps`` output."""

    name: str
    maneuver: str
    distance_miles: float
    duration_seconds: float

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "maneuver": self.maneuver,
            "distance_miles": round(self.distance_miles, 2),
            "duration_seconds": round(self.duration_seconds, 1),
        }


@dataclass(frozen=True)
class RouteResult:
    distance_miles: float
    duration_seconds: float
    geometry: List[List[float]]  # GeoJSON LineString coordinates [lon, lat]
    steps: List[RouteStep] = field(default_factory=list)
    start_snapped: List[float] | None = None
    end_snapped: List[float] | None = None


def format_duration(seconds: float) -> str:
    """Human-readable duration, e.g. ``"12h 10m"`` or ``"45m"``."""
    total_minutes = int(round(seconds / 60.0))
    hours, minutes = divmod(total_minutes, 60)
    if hours > 0:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def _build_url(origin_lon: float, origin_lat: float, dest_lon: float, dest_lat: float) -> str:
    coords = f"{origin_lon:.6f},{origin_lat:.6f};{dest_lon:.6f},{dest_lat:.6f}"
    return f"{settings.OSRM_BASE_URL.rstrip('/')}/route/v1/driving/{coords}"


def _fetch_from_osrm(url: str, params: dict) -> dict:
    try:
        response = _session.get(url, params=params, timeout=REQUEST_TIMEOUT)
    except requests.Timeout:
        logger.warning("OSRM timeout for url %s", url)
        raise ExternalServiceError(
            "The routing service did not respond in time. Please retry."
        )
    except requests.RequestException as exc:
        logger.warning("OSRM connection error: %s", exc)
        raise ExternalServiceError(
            "The routing service could not be reached. Please retry later."
        )

    if response.status_code in (429, 503):
        raise ExternalServiceError(
            "The routing service is rate limiting requests. Please retry shortly."
        )
    if response.status_code != 200:
        raise ExternalServiceError(
            f"The routing service returned an unexpected status ({response.status_code})."
        )

    try:
        return response.json()
    except ValueError as exc:
        raise ExternalServiceError("The routing service returned invalid data.") from exc


def _extract_steps(route: dict) -> List[RouteStep]:
    steps: List[RouteStep] = []
    for leg in route.get("legs", []):
        for step in leg.get("steps", []):
            maneuver = step.get("maneuver", {})
            maneuver_type = maneuver.get("type", "")
            if maneuver_type == "arrive":
                modifier = maneuver.get("modifier", "")
                name = "Arrive at destination" if not modifier else f"Arrive at destination ({modifier})"
            else:
                name = step.get("name") or "(unnamed road)"
            steps.append(
                RouteStep(
                    name=name,
                    maneuver=maneuver_type,
                    distance_miles=step.get("distance", 0.0) / METERS_PER_MILE,
                    duration_seconds=step.get("duration", 0.0),
                )
            )
    return steps


def get_driving_route(origin_lat: float, origin_lon: float, dest_lat: float, dest_lon: float) -> RouteResult:
    """Request the driving route and convert the OSRM payload.

    Raises:
        RoutingError: OSRM answered but no route exists / not drivable.
        ExternalServiceError: OSRM unreachable, slow, or unhealthy.
    """
    url = _build_url(origin_lon, origin_lat, dest_lon, dest_lat)
    params = {"overview": "full", "geometries": "geojson", "steps": "true"}
    data = _fetch_from_osrm(url, params)

    if data.get("code") != "Ok" or not data.get("routes"):
        raise RoutingError(
            "Unable to calculate a driving route between the provided locations."
        )

    route = data["routes"][0]
    geometry = route.get("geometry", {}).get("coordinates", [])
    if len(geometry) < 2:
        raise RoutingError("The routing service returned an empty route geometry.")

    waypoints = data.get("waypoints") or []
    start_snapped = waypoints[0].get("location") if waypoints else None
    end_snapped = waypoints[-1].get("location") if len(waypoints) > 1 else None

    return RouteResult(
        distance_miles=route.get("distance", 0.0) / METERS_PER_MILE,
        duration_seconds=route.get("duration", 0.0),
        geometry=geometry,
        steps=_extract_steps(route),
        start_snapped=start_snapped,
        end_snapped=end_snapped,
    )
