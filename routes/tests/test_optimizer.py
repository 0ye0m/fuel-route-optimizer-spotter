"""Unit tests for the fuel-stop optimization algorithm (no I/O involved)."""

from django.test import SimpleTestCase

from routes.exceptions import NoFuelStationCoverageError
from routes.services.fuel_data import FuelStation
from routes.services.fuel_optimizer import CandidateStation, optimize_fuel_stops

MAX_RANGE = 500.0
MPG = 10.0


def make_station(station_id: str, price: float) -> FuelStation:
    return FuelStation(
        station_id=station_id,
        name=f"Station {station_id}",
        address="1 Highway Ave",
        city="Testville",
        state="KS",
        price_per_gallon=price,
        latitude=40.0,
        longitude=-90.0,
    )


def candidate(station_id: str, price: float, along: float) -> CandidateStation:
    return CandidateStation(
        station=make_station(station_id, price), along_miles=along, off_route_miles=0.0
    )


class NoStopNeededTests(SimpleTestCase):
    def test_trip_within_full_tank_needs_no_stops(self):
        result = optimize_fuel_stops([], total_distance_miles=300.0)
        self.assertEqual(result.purchases, [])
        self.assertEqual(result.total_gallons_purchased, 0.0)
        self.assertEqual(result.total_cost, 0.0)
        self.assertAlmostEqual(result.fuel_remaining_at_destination_gallons, 20.0, places=2)

    def test_trip_of_exactly_max_range_needs_no_stops(self):
        result = optimize_fuel_stops([], total_distance_miles=500.0)
        self.assertEqual(result.purchases, [])
        self.assertAlmostEqual(result.fuel_remaining_at_destination_gallons, 0.0, places=2)


class PurchaseTests(SimpleTestCase):
    def test_purchase_at_single_station_covers_endgame(self):
        # Station at 400 mi on an 800 mi trip: start reaches it with 100 mi
        # of range left, then buy exactly the remaining 300 mi worth.
        candidates = [candidate("A", 3.00, 400.0)]
        result = optimize_fuel_stops(candidates, 800.0, MAX_RANGE, MPG)

        self.assertEqual(len(result.purchases), 1)
        purchase = result.purchases[0]
        self.assertAlmostEqual(purchase.gallons_purchased, 30.0, places=2)
        self.assertAlmostEqual(purchase.fuel_cost, 90.0, places=2)
        self.assertAlmostEqual(purchase.candidate.along_miles, 400.0, places=1)
        self.assertAlmostEqual(result.fuel_remaining_at_destination_gallons, 0.0, places=2)

    def test_defers_to_cheaper_station(self):
        # 800 mi trip; expensive station at 300, cheap one at 650.
        # Optimal: at the first station buy only the 150 mi needed to reach
        # the cheaper one, then buy the rest there.
        candidates = [candidate("A", 3.50, 300.0), candidate("B", 3.00, 650.0)]
        result = optimize_fuel_stops(candidates, 800.0, MAX_RANGE, MPG)

        self.assertEqual(len(result.purchases), 2)
        first, second = result.purchases
        self.assertAlmostEqual(first.candidate.station.price_per_gallon, 3.50)
        self.assertAlmostEqual(first.gallons_purchased, 15.0, places=2)
        self.assertAlmostEqual(first.fuel_cost, 52.50, places=2)
        self.assertAlmostEqual(second.candidate.station.price_per_gallon, 3.00)
        self.assertAlmostEqual(second.gallons_purchased, 15.0, places=2)
        self.assertAlmostEqual(second.fuel_cost, 45.00, places=2)
        self.assertAlmostEqual(result.total_cost, 97.50, places=2)

    def test_prefers_cheaper_of_two_co_located_stations(self):
        candidates = [candidate("A", 3.00, 400.0), candidate("B", 2.50, 400.05)]
        result = optimize_fuel_stops(candidates, 800.0, MAX_RANGE, MPG)
        self.assertEqual(len(result.purchases), 1)
        self.assertAlmostEqual(
            result.purchases[0].candidate.station.price_per_gallon, 2.50
        )

    def test_selects_cheaper_station_when_both_reachable(self):
        # 600 mi trip; stations at 300 ($3.10) and 320 ($3.40). The vehicle
        # can reach both - it must buy at the cheaper one only.
        candidates = [candidate("A", 3.10, 300.0), candidate("B", 3.40, 320.0)]
        result = optimize_fuel_stops(candidates, 600.0, MAX_RANGE, MPG)
        self.assertEqual(len(result.purchases), 1)
        purchase = result.purchases[0]
        self.assertAlmostEqual(purchase.candidate.station.price_per_gallon, 3.10)
        # remaining 300 mi - 200 mi of range left = 100 mi worth
        self.assertAlmostEqual(purchase.gallons_purchased, 10.0, places=2)

    def test_fills_tank_when_no_cheaper_station_is_reachable(self):
        # 1200 mi trip; cheap station at 300, pricier at 700. Nothing cheaper
        # within tank reach of either -> fill at the cheap one, then buy the
        # exact remainder at the pricier one.
        candidates = [candidate("A", 3.00, 300.0), candidate("B", 3.20, 700.0)]
        result = optimize_fuel_stops(candidates, 1200.0, MAX_RANGE, MPG)

        self.assertEqual(len(result.purchases), 2)
        first, second = result.purchases
        self.assertAlmostEqual(first.gallons_purchased, 30.0, places=2)  # fill 200->500 mi
        self.assertAlmostEqual(first.fuel_cost, 90.0, places=2)
        self.assertAlmostEqual(second.gallons_purchased, 40.0, places=2)  # 500-100 mi left -> 400 mi
        self.assertAlmostEqual(second.fuel_cost, 128.0, places=2)
        self.assertAlmostEqual(result.total_cost, 218.0, places=2)


class ConstraintTests(SimpleTestCase):
    def test_no_leg_exceeds_max_range(self):
        candidates = [candidate("A", 3.00, 300.0), candidate("B", 3.20, 700.0)]
        result = optimize_fuel_stops(candidates, 1200.0, MAX_RANGE, MPG)
        positions = [0.0] + result.stop_positions_miles + [1200.0]
        legs = [b - a for a, b in zip(positions, positions[1:])]
        self.assertTrue(all(leg <= MAX_RANGE + 1e-6 for leg in legs))

    def test_starts_with_full_tank_never_buys_at_start(self):
        candidates = [candidate("A", 2.00, 50.0), candidate("B", 1.00, 60.0)]
        result = optimize_fuel_stops(candidates, 400.0, MAX_RANGE, MPG)
        self.assertEqual(result.purchases, [])

    def test_infeasible_gap_raises_coverage_error(self):
        # Station at 300; next usable fuel only at 900 -> a 550+ mi gap.
        candidates = [candidate("A", 3.00, 300.0), candidate("B", 3.00, 900.0)]
        with self.assertRaises(NoFuelStationCoverageError):
            optimize_fuel_stops(candidates, 1500.0, MAX_RANGE, MPG)

    def test_no_stations_and_long_route_raises_coverage_error(self):
        with self.assertRaises(NoFuelStationCoverageError):
            optimize_fuel_stops([], 800.0, MAX_RANGE, MPG)

    def test_station_outside_first_tank_is_unreachable(self):
        # First station beyond max range from the start -> infeasible.
        candidates = [candidate("A", 3.00, 550.0)]
        with self.assertRaises(NoFuelStationCoverageError):
            optimize_fuel_stops(candidates, 900.0, MAX_RANGE, MPG)


class MpgAndDeterminismTests(SimpleTestCase):
    def test_gallons_equal_distance_over_mpg(self):
        candidates = [candidate("A", 3.00, 400.0)]
        result = optimize_fuel_stops(candidates, 800.0, MAX_RANGE, MPG)
        self.assertAlmostEqual(result.total_gallons_purchased, 30.0, places=2)

    def test_cost_is_gallons_times_price(self):
        candidates = [candidate("A", 3.157, 400.0)]
        result = optimize_fuel_stops(candidates, 800.0, MAX_RANGE, MPG)
        expected = round(30.0 * 3.157, 2)
        self.assertAlmostEqual(result.total_cost, expected, places=2)

    def test_optimizer_is_deterministic(self):
        candidates = [candidate("A", 3.50, 300.0), candidate("B", 3.00, 650.0)]
        first = optimize_fuel_stops(candidates, 800.0, MAX_RANGE, MPG)
        second = optimize_fuel_stops(candidates, 800.0, MAX_RANGE, MPG)
        self.assertEqual(
            [p.to_dict() for p in first.purchases], [p.to_dict() for p in second.purchases]
        )
