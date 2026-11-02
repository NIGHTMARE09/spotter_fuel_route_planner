"""Small spherical-geometry helpers (numpy, no GIS dependency)."""

import numpy as np

EARTH_RADIUS_MILES = 3958.7613


def to_xyz(lat, lon):
    """Lat/lon in degrees -> unit-sphere xyz, so nearness is a dot product."""
    lat = np.radians(np.asarray(lat, dtype=float))
    lon = np.radians(np.asarray(lon, dtype=float))
    cos_lat = np.cos(lat)
    return np.column_stack((cos_lat * np.cos(lon), cos_lat * np.sin(lon), np.sin(lat)))


def miles_to_chord(miles):
    """Great-circle distance -> straight-line chord on the unit sphere."""
    return 2.0 * np.sin(np.asarray(miles) / (2.0 * EARTH_RADIUS_MILES))


def chord_to_miles(chord):
    return 2.0 * EARTH_RADIUS_MILES * np.arcsin(np.clip(np.asarray(chord) / 2.0, 0.0, 1.0))


def haversine_miles(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(a))


def densify(coords, step_miles=0.5):
    """Insert points so no segment of a [lon, lat] polyline is longer than `step_miles`.

    OSRM drops vertices on long straight highways; without densifying, the
    nearest *vertex* can be miles from a station that sits right on the road.
    Returns (lat, lon, cumulative_miles) arrays.
    """
    pts = np.asarray(coords, dtype=float)
    lon, lat = pts[:, 0], pts[:, 1]
    seg = haversine_miles(lat[:-1], lon[:-1], lat[1:], lon[1:])
    parts = np.maximum(1, np.ceil(seg / step_miles).astype(int))
    # For segment k, emit `parts[k]` points at fractions 0, 1/parts, ..., (parts-1)/parts.
    seg_idx = np.repeat(np.arange(len(seg)), parts)
    frac = (np.arange(parts.sum()) - np.repeat(np.cumsum(parts) - parts, parts)) / np.repeat(parts, parts)
    d_lat = lat[seg_idx] + (lat[seg_idx + 1] - lat[seg_idx]) * frac
    d_lon = lon[seg_idx] + (lon[seg_idx + 1] - lon[seg_idx]) * frac
    cum_start = np.concatenate(([0.0], np.cumsum(seg)))
    d_cum = cum_start[seg_idx] + seg[seg_idx] * frac
    return (
        np.append(d_lat, lat[-1]),
        np.append(d_lon, lon[-1]),
        np.append(d_cum, cum_start[-1]),
    )


def simplify(coords, tolerance_deg=0.002):
    """Ramer-Douglas-Peucker on [lon, lat] points (planar, fine for display).

    The full OSRM geometry for a coast-to-coast trip is ~1 MB of JSON; this
    keeps the drawn line visually identical at a fraction of the size.
    """
    pts = np.asarray(coords, dtype=float)
    if len(pts) < 3:
        return pts.tolist()
    keep = np.zeros(len(pts), dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        seg = pts[b] - pts[a]
        rel = pts[a + 1 : b] - pts[a]
        norm = np.hypot(*seg)
        if norm == 0:
            dist = np.hypot(rel[:, 0], rel[:, 1])
        else:
            dist = np.abs(seg[0] * rel[:, 1] - seg[1] * rel[:, 0]) / norm
        k = int(np.argmax(dist))
        if dist[k] > tolerance_deg:
            m = a + 1 + k
            keep[m] = True
            stack.append((a, m))
            stack.append((m, b))
    return pts[keep].round(5).tolist()
