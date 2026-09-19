"""Unit tests for the geographic math helpers."""

import math

from django.test import SimpleTestCase

from routes.services.geo_utils import (
    MILES_PER_DEG_LAT,
    densify_route,
    haversine_miles,
    project_points_to_route,
    route_bounding_box,
)


class HaversineTests(SimpleTestCase):
    def test_one_degree_of_longitude_at_equator(self):
        distance = float(haversine_miles(0.0, 0.0, 0.0, 1.0))
        self.assertAlmostEqual(distance, MILES_PER_DEG_LAT, places=2)

    def test_one_degree_of_longitude_at_latitude_40(self):
        expected = MILES_PER_DEG_LAT * math.cos(math.radians(40.0))
        distance = float(haversine_miles(40.0, 0.0, 40.0, 1.0))
        self.assertAlmostEqual(distance, expected, places=2)

    def test_same_point_is_zero(self):
        self.assertEqual(float(haversine_miles(40.0, -80.0, 40.0, -80.0)), 0.0)

    def test_symmetry(self):
        a = float(haversine_miles(40.7, -74.0, 41.9, -87.6))
        b = float(haversine_miles(41.9, -87.6, 40.7, -74.0))
        self.assertAlmostEqual(a, b, places=6)


class DensifyRouteTests(SimpleTestCase):
    def test_cumulative_matches_total_length(self):
        geometry = [[-95.0, 40.0], [-80.0, 40.0]]
        lats, lons, cumulative = densify_route(geometry, step_miles=1.0)
        self.assertEqual(lats[0], 40.0)
        self.assertEqual(lons[-1], -80.0)
        expected_total = 15.0 * MILES_PER_DEG_LAT * math.cos(math.radians(40.0))
        self.assertAlmostEqual(float(cumulative[-1]), expected_total, delta=1.0)
        # monotonic and consistent length
        self.assertEqual(len(lats), len(cumulative))

    def test_step_resolution_respected(self):
        geometry = [[-95.0, 40.0], [-94.0, 40.0]]
        lats, lons, cumulative = densify_route(geometry, step_miles=5.0)
        diffs = cumulative[1:] - cumulative[:-1]
        self.assertTrue(all(float(d) <= 5.0 + 1e-6 for d in diffs))

    def test_requires_two_points(self):
        with self.assertRaises(ValueError):
            densify_route([[-95.0, 40.0]], step_miles=1.0)


class ProjectionTests(SimpleTestCase):
    def setUp(self):
        # 793-mile westward line along latitude 40
        self.geometry = [[-95.0, 40.0], [-80.0, 40.0]]
        self.lats, self.lons, self.cum = densify_route(self.geometry, step_miles=0.25)

    def test_point_on_line_projects_with_zero_offset(self):
        along, off = project_points_to_route(
            [40.0], [-87.5], self.lats, self.lons, self.cum
        )
        self.assertLess(float(off[0]), 0.01)
        # halfway along the line
        self.assertAlmostEqual(float(along[0]), float(self.cum[-1]) / 2.0, delta=0.5)

    def test_point_offset_north_of_line(self):
        offset_deg = 10.0 / MILES_PER_DEG_LAT
        along, off = project_points_to_route(
            [40.0 + offset_deg], [-87.5], self.lats, self.lons, self.cum
        )
        self.assertAlmostEqual(float(off[0]), 10.0, delta=0.15)
        self.assertAlmostEqual(float(along[0]), float(self.cum[-1]) / 2.0, delta=0.5)

    def test_point_before_start_clamps_to_zero(self):
        along, off = project_points_to_route(
            [40.0], [-96.5], self.lats, self.lons, self.cum
        )
        self.assertAlmostEqual(float(along[0]), 0.0, delta=0.5)

    def test_batch_projection_shapes(self):
        along, off = project_points_to_route(
            [40.0, 40.0, 40.5], [-90.0, -85.0, -90.0], self.lats, self.lons, self.cum
        )
        self.assertEqual(along.shape, (3,))
        self.assertEqual(off.shape, (3,))


class BoundingBoxTests(SimpleTestCase):
    def test_margin_expands_box(self):
        geometry = [[-95.0, 40.0], [-80.0, 40.0]]
        min_lat, max_lat, min_lon, max_lon = route_bounding_box(geometry, 25.0)
        self.assertAlmostEqual(min_lat, 40.0 - 25.0 / MILES_PER_DEG_LAT, places=3)
        self.assertLess(min_lon, -95.0)
        self.assertGreater(max_lon, -80.0)
