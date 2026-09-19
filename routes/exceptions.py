"""Domain-specific exceptions with their HTTP status codes.

Services raise these plain exceptions; the DRF exception handler
(``routes.exception_handler``) converts them into JSON error responses.
"""

from __future__ import annotations


class FuelRouteError(Exception):
    """Base class for all domain errors produced by the API."""

    status_code = 500
    default_message = "An unexpected error occurred."

    def __init__(self, message: str | None = None):
        self.message = message or self.default_message
        super().__init__(self.message)


class InvalidRequestError(FuelRouteError):
    """Malformed or logically invalid request (HTTP 400)."""

    status_code = 400
    default_message = "Invalid request."


class SameLocationError(InvalidRequestError):
    """Start and finish resolve to the same location (HTTP 400)."""

    default_message = "Start and finish must be different locations."


class LocationNotFoundError(FuelRouteError):
    """A location could not be geocoded / is outside the USA (HTTP 404)."""

    status_code = 404
    default_message = "Unable to geocode the provided location."


class ExternalServiceError(FuelRouteError):
    """An external service (Nominatim / OSRM) failed or timed out (HTTP 502)."""

    status_code = 502
    default_message = "An external geocoding/routing service is unavailable."


class RoutingError(ExternalServiceError):
    """A driving route could not be calculated (HTTP 502)."""

    default_message = "Unable to calculate a driving route between the locations."


class NoFuelStationCoverageError(FuelRouteError):
    """Route cannot be driven within the vehicle range using available
    fuel stations (HTTP 422)."""

    status_code = 422
    default_message = "No usable fuel stations along the route."


class FuelDataError(FuelRouteError):
    """The fuel-price dataset is missing, malformed or empty (HTTP 500)."""

    status_code = 500
    default_message = "The fuel price dataset could not be loaded."
