"""Geocoding service (Nominatim / OpenStreetMap).

Responsible for turning free-text US location strings into coordinates.
External-HTTP logic lives here - never in the Django views.

* Results are searched globally and verified to be inside the USA.
* Results are cached in the database so each distinct location string costs
  at most one outbound request for the lifetime of the deployment.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import requests
from django.conf import settings

from routes.exceptions import ExternalServiceError, LocationNotFoundError
from routes.models import GeocodingCache

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = settings.EXTERNAL_API_TIMEOUT_SECONDS

# A shared session reuses connections (DNS/TLS handshake per call otherwise
# adds seconds of latency in some hosting environments).
_session = requests.Session()

# Nominatim's usage policy asks for at most one request per second; keep a
# minimum spacing between outbound geocoding calls (configurable, and set to
# 0 in tests).
def _min_interval() -> float:
    return float(getattr(settings, "NOMINATIM_MIN_INTERVAL_SECONDS", 1.1))


_rate_lock = threading.Lock()
_last_request_at = 0.0


def _polite_get(url: str, params: dict, headers: dict) -> requests.Response:
    """Issue a GET while keeping >= interval spacing between Nominatim calls.

    The lock is only held to compute/update the schedule - never across the
    network call - so a slow upstream can never block other requests.
    """
    global _last_request_at
    interval = _min_interval()
    with _rate_lock:
        now = time.monotonic()
        wait = max(0.0, _last_request_at + interval - now)
        _last_request_at = now + wait
    if wait > 0:
        time.sleep(wait)
    return _session.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)


@dataclass(frozen=True)
class GeoLocation:
    """A resolved, verified US location."""

    input: str
    latitude: float
    longitude: float
    display_name: str
    state: str
    city: str = ""

    def to_dict(self) -> dict:
        return {
            "input": self.input,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "display_name": self.display_name,
            "state": self.state,
        }


def _cache_key(query: str) -> str:
    return hashlib.sha256(query.strip().lower().encode("utf-8")).hexdigest()


def _result_is_us(result: dict) -> bool:
    address = result.get("address") or {}
    country_code = (address.get("country_code") or "").lower()
    if country_code:
        return country_code == "us"
    # Fallback when addressdetails is missing from the payload.
    return result.get("display_name", "").rstrip().endswith("United States")


def _extract_state(address: dict) -> str:
    return address.get("state") or ""


def _extract_city(address: dict) -> str:
    for field in ("city", "town", "village", "hamlet", "municipality"):
        if address.get(field):
            return address[field]
    return ""


@lru_cache(maxsize=1)
def _load_local_city_coordinates() -> dict[str, tuple[float, float]]:
    """Load the bundled city table once for fast exact city lookups."""
    path = Path(settings.CITY_COORDINATES_PATH)
    try:
        with path.open(encoding="utf-8") as handle:
            raw = json.load(handle)
        return {str(key).lower(): (float(value[0]), float(value[1])) for key, value in raw.items()}
    except (OSError, TypeError, ValueError, IndexError):
        logger.warning("Local city coordinate table could not be loaded: %s", path)
        return {}


def _local_city_location(query: str) -> GeoLocation | None:
    """Resolve only an unambiguous ``City, two-letter-state`` query locally."""
    if not getattr(settings, "LOCAL_CITY_COORDINATES_ENABLED", True):
        return None

    match = re.fullmatch(r"\s*(.+?)\s*,\s*([A-Za-z]{2})\s*", query)
    if not match:
        return None

    city = match.group(1).strip()
    state = match.group(2).upper()
    if state not in settings.US_STATE_CODES:
        return None

    coordinates = _load_local_city_coordinates().get(f"{city.lower()}|{state.lower()}")
    if coordinates is None:
        return None

    return GeoLocation(
        input=query,
        latitude=coordinates[0],
        longitude=coordinates[1],
        display_name=f"{city}, {state}, United States",
        state=state,
        city=city,
    )


def _fetch_from_nominatim(query: str) -> list:
    """Search Nominatim globally; the caller verifies the result is in the US.

    (We deliberately do NOT pass ``countrycodes=us``: with it, Nominatim
    fuzzy-matches non-US queries like "Toronto, Canada" to random US places.
    Searching globally and then filtering on ``country_code`` gives honest
    "location is not within the United States" errors instead.)
    """
    params = {
        "q": query,
        "format": "json",
        "limit": 5,
        "addressdetails": 1,
    }
    headers = {"User-Agent": settings.NOMINATIM_USER_AGENT}
    url = f"{settings.NOMINATIM_BASE_URL.rstrip('/')}/search"

    try:
        response = _polite_get(url, params, headers)
    except requests.Timeout:
        logger.warning("Nominatim timeout for query %r", query)
        raise ExternalServiceError(
            "The geocoding service did not respond in time. Please retry."
        )
    except requests.RequestException as exc:
        logger.warning("Nominatim connection error for query %r: %s", query, exc)
        raise ExternalServiceError(
            "The geocoding service could not be reached. Please retry later."
        )

    if response.status_code in (429, 503):
        raise ExternalServiceError(
            "The geocoding service is rate limiting requests. Please retry shortly."
        )
    if response.status_code != 200:
        raise ExternalServiceError(
            f"The geocoding service returned an unexpected status "
            f"({response.status_code})."
        )

    try:
        return response.json()
    except ValueError as exc:
        raise ExternalServiceError("The geocoding service returned invalid data.")


def _cache_get(key: str) -> GeocodingCache | None:
    try:
        return GeocodingCache.objects.filter(query_key=key).first()
    except Exception:  # pragma: no cover - cache must never break requests
        logger.exception("Geocoding cache read failed")
        return None


def _cache_put(query: str, key: str, location: GeoLocation) -> None:
    try:
        GeocodingCache.objects.update_or_create(
            query_key=key,
            defaults={
                "query": query,
                "latitude": location.latitude,
                "longitude": location.longitude,
                "display_name": location.display_name,
                "state": location.state,
            },
        )
    except Exception:  # pragma: no cover - cache must never break requests
        logger.exception("Geocoding cache write failed")


def geocode_location(query: str) -> GeoLocation:
    """Geocode a free-text location and guarantee it is inside the USA.

    Raises:
        LocationNotFoundError: nothing usable found / result not in the USA.
        ExternalServiceError: Nominatim unreachable, slow, or unhealthy.
    """
    normalized = " ".join(query.split())
    key = _cache_key(normalized)

    cached = _cache_get(key)
    if cached is not None:
        return GeoLocation(
            input=query,
            latitude=cached.latitude,
            longitude=cached.longitude,
            display_name=cached.display_name,
            state=cached.state,
        )

    local = _local_city_location(normalized)
    if local is not None:
        return local

    results = _fetch_from_nominatim(normalized)

    us_results = [r for r in results if _result_is_us(r)]
    if results and not us_results:
        raise LocationNotFoundError(
            f"'{query}' was found, but it is not within the United States."
        )
    if not us_results:
        raise LocationNotFoundError(f"Unable to geocode the location '{query}'.")

    best = us_results[0]
    address = best.get("address") or {}
    try:
        location = GeoLocation(
            input=query,
            latitude=float(best["lat"]),
            longitude=float(best["lon"]),
            display_name=best.get("display_name", ""),
            state=_extract_state(address),
            city=_extract_city(address),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ExternalServiceError(
            "The geocoding service returned a malformed result."
        ) from exc

    _cache_put(normalized, key, location)
    return location
