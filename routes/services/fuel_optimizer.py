"""Fuel-stop matching and cost-optimal refueling strategy.

Two responsibilities live here:

1. ``find_candidate_stations`` - match CSV fuel stations to the calculated
   route: every station is projected onto the densified route polyline; only
   stations within ``FUEL_STATION_SEARCH_RADIUS_MILES`` of the route become
   candidates, each annotated with its position along the route and its
   distance off the route.

2. ``optimize_fuel_stops`` - the cost-minimizing refueling plan.

Optimization strategy (greedy with look-ahead, provably optimal for the
"gas station problem" with bounded tank capacity):

* The vehicle starts at route position 0 with a full tank (``max_range``
  miles of range) and burns ``distance / mpg`` gallons per segment.
* At every decision point the optimizer looks at all candidate stations
  within one full tank ahead (``max_range``):
    a. If the remaining trip distance fits inside the current fuel, stop
       optimizing - no purchase is needed.
    b. If a *strictly cheaper* station exists within one full tank, buy at
       the current station only the fuel needed to reach the *nearest* such
       station, then re-evaluate there. (Buying more than that at a more
       expensive station is never beneficial.)
    c. Otherwise, if the remainder of the trip fits inside one full tank,
       buy exactly the fuel needed to finish (cheapest possible ending).
    d. Otherwise (must use pricier stations later), fill the tank to
       capacity - every gallon bought at the current (cheapest-available)
       price displaces a gallon bought at a higher price - and drive to the
       cheapest reachable station.
* Stations at (nearly) the same along-route position are collapsed to the
  cheapest one so purchase decisions are unambiguous.
* Infeasibility (a gap of more than ``max_range`` miles without usable
  stations) raises ``NoFuelStationCoverageError``.

The result is deterministic: identical inputs always produce identical plans.
"""

from __future__ import annotations

import logging
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import List, Sequence

from django.conf import settings

from routes.exceptions import NoFuelStationCoverageError
from routes.services.fuel_data import FuelStation
from routes.services.geo_utils import (
    densify_route,
    project_points_to_route,
    route_bounding_box,
)

logger = logging.getLogger(__name__)

# Stations whose projections differ by less than this are treated as one
# stop (projection noise / co-located stations in the same city).
CO_LOCATED_EPSILON_MILES = 0.05


@dataclass(frozen=True)
class CandidateStation:
    """A fuel station matched to the route corridor."""

    station: FuelStation
    along_miles: float  # position along the route (from the start)
    off_route_miles: float  # perpendicular distance from the route

    def to_dict(self) -> dict:
        return {
            **self.station.to_dict(),
            "along_route_miles": round(self.along_miles, 1),
            "distance_from_route_miles": round(self.off_route_miles, 2),
        }


@dataclass(frozen=True)
class FuelPurchase:
    """One planned refuel."""

    sequence: int
    candidate: CandidateStation
    gallons_purchased: float
    fuel_cost: float
    reason: str

    def to_dict(self) -> dict:
        station = self.candidate.station
        return {
            "sequence": self.sequence,
            "station_id": station.station_id,
            "station_name": station.name,
            "address": station.address,
            "city": station.city,
            "state": station.state,
            "latitude": station.latitude,
            "longitude": station.longitude,
            "price_per_gallon": round(station.price_per_gallon, 4),
            "distance_from_start_miles": round(self.candidate.along_miles, 1),
            "distance_from_route_miles": round(self.candidate.off_route_miles, 2),
            "gallons_purchased": round(self.gallons_purchased, 2),
            "fuel_cost": round(self.fuel_cost, 2),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class OptimizationResult:
    purchases: List[FuelPurchase] = field(default_factory=list)
    total_gallons_purchased: float = 0.0
    total_cost: float = 0.0
    fuel_remaining_at_destination_gallons: float = 0.0
    stop_positions_miles: List[float] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Route/station matching
# ---------------------------------------------------------------------------

def find_candidate_stations(
    stations: Sequence[FuelStation],
    route_geometry: Sequence[Sequence[float]],
    route_distance_miles: float,
    search_radius_miles: float | None = None,
    densify_step_miles: float | None = None,
) -> tuple[List[CandidateStation], dict]:
    """Return stations within the route corridor, ordered along the route.

    Performance: the route is densified once; stations far outside the route
    bounding box are discarded before any per-station math; the remaining
    projections run in vectorized numpy chunks. No external API is involved.
    """
    radius = float(
        search_radius_miles
        if search_radius_miles is not None
        else settings.FUEL_STATION_SEARCH_RADIUS_MILES
    )
    step = float(
        densify_step_miles
        if densify_step_miles is not None
        else settings.ROUTE_DENSIFY_STEP_MILES
    )

    route_lats, route_lons, cumulative = densify_route(route_geometry, step)
    # Make along-route distances consistent with the authoritative OSRM
    # distance (densified great-circle sum can differ by a fraction of a %).
    if cumulative[-1] > 0:
        cumulative *= route_distance_miles / cumulative[-1]

    min_lat, max_lat, min_lon, max_lon = route_bounding_box(route_geometry, radius)

    lats, lons, indices = [], [], []
    for index, station in enumerate(stations):
        if min_lat <= station.latitude <= max_lat and min_lon <= station.longitude <= max_lon:
            lats.append(station.latitude)
            lons.append(station.longitude)
            indices.append(index)

    stats = {
        "stations_total": len(stations),
        "stations_in_route_bounding_box": len(indices),
    }

    if not lats:
        stats["stations_within_corridor"] = 0
        return [], stats

    along, off = project_points_to_route(lats, lons, route_lats, route_lons, cumulative)

    candidates: List[CandidateStation] = []
    for position, index in enumerate(indices):
        if off[position] <= radius:
            candidates.append(
                CandidateStation(
                    station=stations[index],
                    along_miles=float(along[position]),
                    off_route_miles=float(off[position]),
                )
            )

    candidates.sort(key=lambda c: (c.along_miles, c.station.price_per_gallon))
    stats["stations_within_corridor"] = len(candidates)
    return candidates, stats


# ---------------------------------------------------------------------------
# Optimization
# ---------------------------------------------------------------------------

def optimize_fuel_stops(
    candidates: Sequence[CandidateStation],
    total_distance_miles: float,
    max_range_miles: float | None = None,
    fuel_efficiency_mpg: float | None = None,
) -> OptimizationResult:
    """Compute the cost-minimizing refuel plan (see module docstring)."""
    max_range = float(
        max_range_miles if max_range_miles is not None else settings.MAX_RANGE_MILES
    )
    mpg = float(
        fuel_efficiency_mpg
        if fuel_efficiency_mpg is not None
        else settings.FUEL_EFFICIENCY_MPG
    )

    if max_range <= 0 or mpg <= 0:
        raise ValueError("max_range_miles and fuel_efficiency_mpg must be positive.")

    sorted_candidates = sorted(
        candidates, key=lambda c: (c.along_miles, c.station.price_per_gallon)
    )
    alongs = [c.along_miles for c in sorted_candidates]

    purchases: List[FuelPurchase] = []
    position = 0.0
    range_left = max_range  # starting with a full tank
    current_price: float | None = None  # cannot buy at the start (tank full)
    current_station: CandidateStation | None = None

    max_iterations = 4 * (len(sorted_candidates) + 2)
    iterations = 0

    while True:
        iterations += 1
        if iterations > max_iterations:  # defensive guard, cannot normally happen
            raise NoFuelStationCoverageError(
                "Fuel-stop optimization failed to converge; route corridor data "
                "may be inconsistent."
            )

        remaining = total_distance_miles - position
        if remaining <= range_left + 1e-9:
            break  # destination reachable with current fuel

        # Stations strictly ahead that are reachable with one full tank.
        start_index = bisect_right(alongs, position + 1e-9)
        end_index = bisect_right(alongs, position + max_range + 1e-9)
        ahead = sorted_candidates[start_index:end_index]

        if not ahead:
            if current_price is not None and remaining <= max_range + 1e-9:
                # No station ahead, but the remainder fits in one tank:
                # buy exactly what is needed to finish here.
                needed_miles = remaining - range_left
                _record_purchase(
                    purchases,
                    current_station,
                    current_price,
                    needed_miles,
                    mpg,
                    "Bought exactly enough fuel to reach the destination (no "
                    "further stations along the route).",
                )
                range_left -= needed_miles
                position = total_distance_miles
                break
            raise NoFuelStationCoverageError(
                f"No fuel station within {max_range:.0f} miles of route position "
                f"{position:.0f} mi; the remaining {remaining:.0f} miles cannot "
                f"be covered with the available stations."
            )

        # Collapse co-located stations to the cheapest at that position.
        first_along = ahead[0].along_miles
        cluster = [
            c for c in ahead if c.along_miles - first_along <= CO_LOCATED_EPSILON_MILES
        ]

        if current_price is None:
            # At the start we cannot buy (tank already full): drive to the
            # nearest station; if several share the position, the cheapest.
            target = min(cluster, key=lambda c: (c.station.price_per_gallon, c.along_miles))
            range_left -= target.along_miles - position
            position = target.along_miles
            current_price = target.station.price_per_gallon
            current_station = target
            continue

        cheaper = [
            c for c in ahead if c.station.price_per_gallon < current_price - 1e-9
        ]
        if cheaper:
            target = min(cheaper, key=lambda c: (c.along_miles, c.station.price_per_gallon))
            needed_miles = max(0.0, target.along_miles - position - range_left)
            _record_purchase(
                purchases,
                current_station,
                current_price,
                needed_miles,
                mpg,
                (
                    f"Bought only enough fuel to reach the next cheaper station "
                    f"'{target.station.name}' (${target.station.price_per_gallon:.2f}/gal) "
                    f"{target.along_miles - position:.0f} mi ahead."
                ),
            )
            range_left = range_left + needed_miles - (target.along_miles - position)
            position = target.along_miles
            current_price = target.station.price_per_gallon
            current_station = target
            continue

        if remaining <= max_range + 1e-9:
            # Endgame: nothing cheaper within reach and the rest of the trip
            # fits in one tank - buy exactly what is needed to finish.
            needed_miles = remaining - range_left
            _record_purchase(
                purchases,
                current_station,
                current_price,
                needed_miles,
                mpg,
                "Bought exactly enough fuel to reach the destination (no cheaper "
                "station within range).",
            )
            range_left -= needed_miles
            position = total_distance_miles
            break

        # Must continue past one tank range: fill the tank (cheapest price
        # available from here) and head to the cheapest reachable station.
        needed_miles = max_range - range_left
        _record_purchase(
            purchases,
            current_station,
            current_price,
            needed_miles,
            mpg,
            "No cheaper station within one tank range - filled the tank at the "
            "cheapest price available and headed to the cheapest reachable "
            "station ahead.",
        )
        range_left = max_range
        target = min(ahead, key=lambda c: (c.station.price_per_gallon, c.along_miles))
        range_left -= target.along_miles - position
        position = target.along_miles
        current_price = target.station.price_per_gallon
        current_station = target

    total_gallons = sum(p.gallons_purchased for p in purchases)
    total_cost = sum(p.fuel_cost for p in purchases)
    # Fuel still in the tank when the destination is reached.
    if position < total_distance_miles:
        range_left -= total_distance_miles - position
    return OptimizationResult(
        purchases=purchases,
        total_gallons_purchased=round(total_gallons, 2),
        total_cost=round(total_cost, 2),
        fuel_remaining_at_destination_gallons=round(max(range_left, 0.0) / mpg, 2),
        stop_positions_miles=[p.candidate.along_miles for p in purchases],
    )


def _record_purchase(
    purchases: List[FuelPurchase],
    station: CandidateStation | None,
    price: float,
    needed_miles: float,
    mpg: float,
    reason: str,
) -> None:
    """Append a purchase record (rounded for display consistency)."""
    if station is None or needed_miles <= 1e-9:
        return
    gallons = round(needed_miles / mpg, 2)
    if gallons <= 0:
        return
    purchases.append(
        FuelPurchase(
            sequence=len(purchases) + 1,
            candidate=station,
            gallons_purchased=gallons,
            fuel_cost=round(gallons * price, 2),
            reason=reason,
        )
    )
