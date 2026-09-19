"""Geographic helper utilities.

Pure ``math``/``numpy`` implementations (no external GIS dependencies) of the
three operations the optimizer needs:

1. ``haversine_miles``          – great-circle distance,
2. ``densify_route``            – interpolate the route polyline at a fixed
                                   spatial resolution,
3. ``project_points_to_route``  – for many points at once, find where each one
                                   best "attaches" to the route polyline and
                                   return (position along the route, distance
                                   off the route).
"""

from __future__ import annotations

import math
from typing import List, Sequence, Tuple

import numpy as np

EARTH_RADIUS_MILES = 3958.7613
# Length of one degree of latitude in miles (used for local equirectangular
# scaling; accurate enough for corridor-scale distances).
MILES_PER_DEG_LAT = math.pi / 180.0 * EARTH_RADIUS_MILES  # ~69.05


def haversine_miles(lat1, lon1, lat2, lon2):
    """Great-circle distance in miles between two points (or point arrays)."""
    lat1r = np.radians(lat1)
    lat2r = np.radians(lat2)
    dlat = lat2r - lat1r
    dlon = np.radians(np.subtract(lon2, lon1))
    a = (
        np.sin(dlat / 2.0) ** 2
        + np.cos(lat1r) * np.cos(lat2r) * np.sin(dlon / 2.0) ** 2
    )
    return 2.0 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def route_bounding_box(
    geometry: Sequence[Sequence[float]], margin_miles: float
) -> Tuple[float, float, float, float]:
    """Bounding box (min_lat, max_lat, min_lon, max_lon) of a GeoJSON
    LineString, expanded by ``margin_miles``. Used to cheaply discard fuel
    stations that cannot possibly be near the route."""
    lats = [float(c[1]) for c in geometry]
    lons = [float(c[0]) for c in geometry]
    max_abs_lat = max(abs(min(lats)), abs(max(lats)))
    cos_lat = max(0.05, math.cos(math.radians(min(89.0, max_abs_lat))))
    dlat = margin_miles / MILES_PER_DEG_LAT
    dlon = margin_miles / (MILES_PER_DEG_LAT * cos_lat)
    return (min(lats) - dlat, max(lats) + dlat, min(lons) - dlon, max(lons) + dlon)


def densify_route(
    geometry: Sequence[Sequence[float]], step_miles: float
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Interpolate a GeoJSON LineString (``[[lon, lat], ...]``) so that
    consecutive points are at most ``step_miles`` apart.

    Returns ``(lats, lons, cumulative_miles)`` where ``cumulative_miles[i]``
    is the along-route distance of point ``i`` from the route start.

    Linear interpolation inside a segment is accurate at this resolution
    (segments are far shorter than the scale at which the earth's curvature
    matters for corridor matching).
    """
    if len(geometry) < 2:
        raise ValueError("Route geometry must contain at least two coordinates.")

    lats: List[float] = [float(geometry[0][1])]
    lons: List[float] = [float(geometry[0][0])]
    cumulative: List[float] = [0.0]
    total = 0.0

    for (lon1, lat1), (lon2, lat2) in zip(geometry, geometry[1:]):
        seg_miles = float(haversine_miles(lat1, lon1, lat2, lon2))
        if seg_miles <= 0.0:
            continue
        steps = max(1, int(math.ceil(seg_miles / step_miles)))
        for k in range(1, steps + 1):
            frac = k / steps
            lats.append(float(lat1) + (float(lat2) - float(lat1)) * frac)
            lons.append(float(lon1) + (float(lon2) - float(lon1)) * frac)
            total += seg_miles / steps
            cumulative.append(total)

    return (
        np.asarray(lats, dtype=float),
        np.asarray(lons, dtype=float),
        np.asarray(cumulative, dtype=float),
    )


def _project_onto_segment(
    q_lat: float,
    q_lon: float,
    p1_lat: float,
    p1_lon: float,
    p2_lat: float,
    p2_lon: float,
    cum_start: float,
) -> Tuple[float, float]:
    """Project point (q_lat, q_lon) onto segment p1->p2 in a local planar
    frame (equirectangular miles around the query latitude).

    Returns ``(along_miles, perp_miles)``; ``along_miles`` is measured from
    the route start (``cum_start`` is the cumulative distance at p1) and is
    clamped to the segment.
    """
    cos_lat = max(0.05, math.cos(math.radians(q_lat)))
    scale = MILES_PER_DEG_LAT

    ax, ay = p1_lon * scale * cos_lat, p1_lat * scale
    bx, by = p2_lon * scale * cos_lat, p2_lat * scale
    qx, qy = q_lon * scale * cos_lat, q_lat * scale

    seg_len = float(haversine_miles(p1_lat, p1_lon, p2_lat, p2_lon))
    if seg_len <= 0.0:
        return cum_start, float(haversine_miles(q_lat, q_lon, p1_lat, p1_lon))

    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    t = ((qx - ax) * dx + (qy - ay) * dy) / denom if denom > 0 else 0.0
    t = max(0.0, min(1.0, t))

    proj_x, proj_y = ax + t * dx, ay + t * dy
    perp = math.hypot(qx - proj_x, qy - proj_y)
    along = cum_start + t * seg_len
    return along, perp


def project_points_to_route(
    query_lats: Sequence[float],
    query_lons: Sequence[float],
    route_lats: np.ndarray,
    route_lons: np.ndarray,
    cumulative_miles: np.ndarray,
    chunk_size: int = 512,
) -> Tuple[np.ndarray, np.ndarray]:
    """Match query points to the densified route polyline (vectorized).

    For every query point this finds the closest densified route point
    (coarse, via a chunked distance matrix) and then refines the result by
    exactly projecting the point onto the two segments adjacent to that
    point. Returns ``(along_miles, off_route_miles)`` arrays.
    """
    q_lats = np.asarray(query_lats, dtype=float)
    q_lons = np.asarray(query_lons, dtype=float)
    n_points = q_lats.size
    n_route = route_lats.size

    along_result = np.zeros(n_points, dtype=float)
    off_result = np.zeros(n_points, dtype=float)

    rlats = route_lats[None, :]
    rlons = route_lons[None, :]
    rcum = cumulative_miles

    for start in range(0, n_points, chunk_size):
        stop = min(start + chunk_size, n_points)
        qla = q_lats[start:stop, None]
        qlo = q_lons[start:stop, None]

        # Equirectangular approximation - plenty accurate for finding the
        # nearest densified point, which the exact projection then refines.
        lat_mid = np.radians((qla + rlats) / 2.0)
        dx = (rlons - qlo) * MILES_PER_DEG_LAT * np.cos(lat_mid)
        dy = (rlats - qla) * MILES_PER_DEG_LAT
        dist2 = dx * dx + dy * dy
        nearest_idx = np.argmin(dist2, axis=1)

        for row, idx in enumerate(nearest_idx):
            i = int(idx)
            q_lat = float(q_lats[start + row])
            q_lon = float(q_lons[start + row])

            best_along = float(rcum[i])
            best_off = float(
                haversine_miles(q_lat, q_lon, route_lats[i], route_lons[i])
            )

            for seg_start in (i - 1, i):
                if seg_start < 0 or seg_start + 1 >= n_route:
                    continue
                seg_along, seg_off = _project_onto_segment(
                    q_lat,
                    q_lon,
                    float(route_lats[seg_start]),
                    float(route_lons[seg_start]),
                    float(route_lats[seg_start + 1]),
                    float(route_lons[seg_start + 1]),
                    float(rcum[seg_start]),
                )
                if seg_off < best_off:
                    best_off = seg_off
                    best_along = seg_along

            along_result[start + row] = best_along
            off_result[start + row] = best_off

    return along_result, off_result
