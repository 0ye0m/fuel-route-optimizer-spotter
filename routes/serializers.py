"""DRF serializers for the fuel-route API."""

from __future__ import annotations

from rest_framework import serializers


class RouteRequestSerializer(serializers.Serializer):
    """Validates POST /api/route/ request bodies."""

    start = serializers.CharField(
        required=True,
        max_length=200,
        trim_whitespace=True,
        help_text="Free-text US start location, e.g. 'New York, NY'.",
    )
    finish = serializers.CharField(
        required=True,
        max_length=200,
        trim_whitespace=True,
        help_text="Free-text US finish location, e.g. 'Chicago, IL'.",
    )

    def validate_start(self, value: str) -> str:
        if not value.strip():
            raise serializers.ValidationError("Start location must not be empty.")
        return value.strip()

    def validate_finish(self, value: str) -> str:
        if not value.strip():
            raise serializers.ValidationError("Finish location must not be empty.")
        return value.strip()

    def validate(self, attrs):
        if attrs["start"].strip().lower() == attrs["finish"].strip().lower():
            raise serializers.ValidationError(
                "Start and finish must be different locations."
            )
        return attrs


# ---------------------------------------------------------------------------
# Response serializers - they document (and enforce) the response contract.
# ---------------------------------------------------------------------------


class GeoPointSerializer(serializers.Serializer):
    input = serializers.CharField()
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()
    display_name = serializers.CharField()
    state = serializers.CharField(allow_blank=True)


class TripSerializer(serializers.Serializer):
    start = GeoPointSerializer()
    finish = GeoPointSerializer()
    distance_miles = serializers.FloatField()
    duration_minutes = serializers.IntegerField()
    duration_text = serializers.CharField()


class VehicleSerializer(serializers.Serializer):
    max_range_miles = serializers.FloatField()
    fuel_efficiency_mpg = serializers.FloatField()
    fuel_capacity_gallons = serializers.FloatField()


class FuelStopSerializer(serializers.Serializer):
    sequence = serializers.IntegerField()
    station_id = serializers.CharField(allow_blank=True)
    station_name = serializers.CharField()
    address = serializers.CharField(allow_blank=True)
    city = serializers.CharField(allow_blank=True)
    state = serializers.CharField(allow_blank=True)
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()
    price_per_gallon = serializers.FloatField()
    distance_from_start_miles = serializers.FloatField()
    distance_from_route_miles = serializers.FloatField()
    gallons_purchased = serializers.FloatField()
    fuel_cost = serializers.FloatField()
    reason = serializers.CharField()


class FuelSummarySerializer(serializers.Serializer):
    total_distance_miles = serializers.FloatField()
    total_fuel_consumed_gallons = serializers.FloatField()
    total_fuel_purchased_gallons = serializers.FloatField()
    total_fuel_cost = serializers.FloatField()
    fuel_remaining_at_destination_gallons = serializers.FloatField()
    stops_required = serializers.IntegerField()
    note = serializers.CharField()


class RouteGeometrySerializer(serializers.Serializer):
    type = serializers.CharField()
    coordinates = serializers.ListField(
        child=serializers.ListField(child=serializers.FloatField()), allow_empty=False
    )


class RouteStepSerializer(serializers.Serializer):
    name = serializers.CharField()
    maneuver = serializers.CharField()
    distance_miles = serializers.FloatField()
    duration_seconds = serializers.FloatField()


class RouteDetailSerializer(serializers.Serializer):
    geometry = RouteGeometrySerializer()
    start_snapped = serializers.ListField(
        child=serializers.FloatField(), allow_null=True
    )
    end_snapped = serializers.ListField(child=serializers.FloatField(), allow_null=True)
    steps = RouteStepSerializer(many=True)


class OptimizationMetadataSerializer(serializers.Serializer):
    fuel_stations_in_dataset = serializers.IntegerField()
    stations_in_route_bounding_box = serializers.IntegerField()
    stations_within_corridor = serializers.IntegerField()
    corridor_radius_miles = serializers.FloatField()
    algorithm = serializers.CharField()


class RoutePlanResponseSerializer(serializers.Serializer):
    """Top-level response body of POST /api/route/."""

    trip = TripSerializer()
    vehicle = VehicleSerializer()
    fuel_stops = FuelStopSerializer(many=True)
    fuel_summary = FuelSummarySerializer()
    route = RouteDetailSerializer()
    metadata = OptimizationMetadataSerializer()
