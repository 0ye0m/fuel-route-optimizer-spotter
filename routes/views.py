"""API views.

The view stays thin: it validates input with a DRF serializer, orchestrates
the service layer (geocoding -> routing -> fuel matching/optimization) and
shapes the JSON response. All business logic lives in ``routes.services``.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from django.conf import settings
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from routes.exceptions import FuelRouteError, SameLocationError
from routes.serializers import (
    RoutePlanResponseSerializer,
    RouteRequestSerializer,
)
from routes.services.fuel_data import get_fuel_data_service
from routes.services.fuel_optimizer import find_candidate_stations, optimize_fuel_stops
from routes.services.geo_utils import haversine_miles
from routes.services.geocoding import geocode_location
from routes.services.routing import format_duration, get_driving_route

logger = logging.getLogger(__name__)

# Two geocoded points closer than this are considered "the same place".
SAME_LOCATION_TOLERANCE_MILES = 0.25


class RoutePlanAPIView(APIView):
    """POST /api/route/ - plan a fuel-optimized trip between two US locations."""

    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        request_serializer = RouteRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        start_query = request_serializer.validated_data["start"]
        finish_query = request_serializer.validated_data["finish"]

        try:
            payload = self._build_route_plan(start_query, finish_query)
        except FuelRouteError:
            # Domain errors carry their own HTTP status; the DRF exception
            # handler renders them as {"error": ...}.
            raise
        except Exception:
            logger.exception("Unexpected error planning route %s -> %s", start_query, finish_query)
            return Response(
                {"error": "An unexpected internal error occurred. Please retry."},
                status=500,
            )

        # Enforce/document the response contract.
        response_serializer = RoutePlanResponseSerializer(instance=payload)
        return Response(response_serializer.data, status=200)

    def get(self, request, *args, **kwargs):
        """Small self-describing endpoint (useful for quick manual checks)."""
        return Response(
            {
                "endpoint": "POST /api/route/",
                "description": (
                    "Plans a driving route between two US locations and "
                    "computes the cost-optimal fuel stops along it."
                ),
                "request_body": {"start": "New York, NY", "finish": "Chicago, IL"},
                "vehicle": {
                    "max_range_miles": settings.MAX_RANGE_MILES,
                    "fuel_efficiency_mpg": settings.FUEL_EFFICIENCY_MPG,
                },
            },
            status=200,
        )

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------
    def _build_route_plan(self, start_query: str, finish_query: str) -> dict:
        if getattr(settings, "ROUTE_PLAN_CACHE_ENABLED", True):
            return self._cached_route_plan(start_query, finish_query)
        return self._build_route_plan_uncached(start_query, finish_query)

    @staticmethod
    @lru_cache(maxsize=getattr(settings, "ROUTE_PLAN_CACHE_MAX_ENTRIES", 64))
    def _cached_route_plan(start_query: str, finish_query: str) -> dict:
        return RoutePlanAPIView()._build_route_plan_uncached(start_query, finish_query)

    def _build_route_plan_uncached(self, start_query: str, finish_query: str) -> dict:
        origin = geocode_location(start_query)
        destination = geocode_location(finish_query)

        if (
            haversine_miles(
                origin.latitude, origin.longitude, destination.latitude, destination.longitude
            )
            < SAME_LOCATION_TOLERANCE_MILES
        ):
            raise SameLocationError(
                "Start and finish resolve to the same location. "
                "Please choose two different places."
            )

        route = get_driving_route(
            origin.latitude, origin.longitude, destination.latitude, destination.longitude
        )

        fuel_service = get_fuel_data_service()
        stations = fuel_service.get_stations()
        fuel_stats = fuel_service.get_stats()

        candidates, corridor_stats = find_candidate_stations(
            stations, route.geometry, route.distance_miles
        )
        optimization = optimize_fuel_stops(
            candidates, route.distance_miles
        )

        total_consumed = round(route.distance_miles / settings.FUEL_EFFICIENCY_MPG, 2)
        stops_payload = [purchase.to_dict() for purchase in optimization.purchases]

        if optimization.purchases:
            summary_note = (
                "The vehicle starts with a full tank (50 gallons / 500 miles). "
                "Fuel consumed covers the whole trip; fuel purchased is only the "
                "fuel actually bought at the stops below."
            )
        else:
            summary_note = (
                f"No intermediate fuel stop is required: the route "
                f"({route.distance_miles:.1f} miles) fits within the vehicle's "
                f"{settings.MAX_RANGE_MILES:.0f}-mile tank range. The vehicle "
                f"starts with a full tank, so although {total_consumed} gallons "
                f"are consumed, no fuel needs to be purchased and the trip's "
                f"direct fuel purchase cost is $0.00."
            )

        return {
            "trip": {
                "start": origin.to_dict(),
                "finish": destination.to_dict(),
                "distance_miles": round(route.distance_miles, 1),
                "duration_minutes": int(round(route.duration_seconds / 60.0)),
                "duration_text": format_duration(route.duration_seconds),
            },
            "vehicle": {
                "max_range_miles": settings.MAX_RANGE_MILES,
                "fuel_efficiency_mpg": settings.FUEL_EFFICIENCY_MPG,
                "fuel_capacity_gallons": round(
                    settings.MAX_RANGE_MILES / settings.FUEL_EFFICIENCY_MPG, 2
                ),
            },
            "fuel_stops": stops_payload,
            "fuel_summary": {
                "total_distance_miles": round(route.distance_miles, 1),
                "total_fuel_consumed_gallons": total_consumed,
                "total_fuel_purchased_gallons": optimization.total_gallons_purchased,
                "total_fuel_cost": optimization.total_cost,
                "fuel_remaining_at_destination_gallons": (
                    optimization.fuel_remaining_at_destination_gallons
                ),
                "stops_required": len(optimization.purchases),
                "note": summary_note,
            },
            "route": {
                "geometry": {
                    "type": "LineString",
                    "coordinates": route.geometry,
                },
                "start_snapped": route.start_snapped,
                "end_snapped": route.end_snapped,
                "steps": [step.to_dict() for step in route.steps],
            },
            "metadata": {
                "fuel_stations_in_dataset": fuel_stats.get("usable_stations_loaded", 0),
                "stations_in_route_bounding_box": corridor_stats.get(
                    "stations_in_route_bounding_box", 0
                ),
                "stations_within_corridor": corridor_stats.get(
                    "stations_within_corridor", 0
                ),
                "corridor_radius_miles": settings.FUEL_STATION_SEARCH_RADIUS_MILES,
                "algorithm": (
                    "Greedy gas-station optimization with look-ahead: buy only "
                    "enough fuel to reach the next cheaper station when one is "
                    "reachable within tank range; otherwise fill the tank (or "
                    "buy exactly what the remainder of the trip needs)."
                ),
            },
        }
