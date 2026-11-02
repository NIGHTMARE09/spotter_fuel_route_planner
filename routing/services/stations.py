"""In-memory spatial index of fuel stations.

~6.6k stations fit easily in memory, so we load them once per process into
numpy arrays and answer "which stations are near this route, and at what
mile marker" without touching the database on the hot path.

Points are unit vectors on the sphere, so "closeness" is a dot product and
the whole search is two small matrix multiplications:
  1. stations x route sampled every 5 mi    -> drop everything far away
  2. survivors x every route point (<= 0.5 mi apart), in column chunks so a
     35k-vertex coast-to-coast route never builds a 100+ MB matrix
     -> exact nearest point + mile

The index reloads itself when the station table changes (checked at most
every VERSION_CHECK_SECONDS), so `load_stations` takes effect in a running
server without a restart.
"""

import threading
import time
from dataclasses import dataclass

import numpy as np

from .geo import chord_to_miles, densify, miles_to_chord, to_xyz

DENSE_STEP_MILES = 0.5
COARSE_STEP_MILES = 5.0
CHUNK_POINTS = 4096  # route points per stage-2 block: ~500 stations x 4096 x 8 B = 16 MB
VERSION_CHECK_SECONDS = 5.0


class StationsUnavailable(RuntimeError):
    """The station table is empty: `python manage.py load_stations` has not been run."""


def _min_dot(miles):
    """Smallest dot product two unit vectors can have while within `miles` of each other."""
    chord = miles_to_chord(miles)
    return 1.0 - chord * chord / 2.0


@dataclass(frozen=True)
class StationOnRoute:
    station_id: int
    mile: float  # distance from the start along the route
    off_route_miles: float  # straight-line distance from the route line
    price: float


class StationIndex:
    def __init__(self, ids, lats, lons, prices, version=""):
        self.version = version
        self.ids = np.asarray(ids, dtype=np.int64)
        self.prices = np.asarray(prices, dtype=float)
        self.xyz = to_xyz(lats, lons)

    def along_route(self, coords, corridor_miles):
        """Stations within `corridor_miles` of a [lon, lat] polyline, ordered by mile marker."""
        r_lat, r_lon, r_cum = densify(coords, step_miles=DENSE_STEP_MILES)
        route = to_xyz(r_lat, r_lon)

        # Stage 1: the first point at or after every 5-mile mark. Points are
        # <= 0.5 mi apart, so every point is within COARSE_STEP/2 + 0.5 path
        # miles of a sample; widening the corridor by that never drops a hit.
        marks = np.searchsorted(r_cum, np.arange(0.0, r_cum[-1], COARSE_STEP_MILES))
        coarse = route[np.unique(np.append(marks, len(route) - 1))]
        near = (self.xyz @ coarse.T >= _min_dot(corridor_miles + COARSE_STEP_MILES / 2 + 0.5)).any(axis=1)
        cand = np.flatnonzero(near)

        # Stage 2: exact nearest route point for the few hundred survivors.
        pts = self.xyz[cand]
        rows = np.arange(len(cand))
        best = np.full(len(cand), -np.inf)
        nearest = np.zeros(len(cand), dtype=np.intp)
        for a in range(0, len(route), CHUNK_POINTS):
            dots = pts @ route[a : a + CHUNK_POINTS].T
            k = dots.argmax(axis=1)
            v = dots[rows, k]
            better = v > best
            best[better], nearest[better] = v[better], a + k[better]
        keep = best >= _min_dot(corridor_miles)
        sel, nearest, best = cand[keep], nearest[keep], best[keep]

        off = chord_to_miles(np.sqrt(np.maximum(0.0, 2.0 - 2.0 * best)))

        # Stations geocoded to the same city centroid snap to the same route point
        # (64% of stations share a coordinate, up to 22 at one point). Only the
        # cheapest there can matter to the optimizer; ties go to the closest.
        prices = self.prices[sel]
        order = np.lexsort((off, prices, nearest))  # by point, then price, then distance
        _, first = np.unique(nearest[order], return_index=True)
        pick = order[first]  # already ordered by route point, i.e. by mile marker

        miles = r_cum[nearest[pick]]
        return [
            StationOnRoute(int(i), float(m), float(o), float(p))
            for i, m, o, p in zip(self.ids[sel][pick], miles, off[pick], prices[pick])
        ]


_index = None
_checked_at = 0.0
_lock = threading.Lock()


def _table_version():
    """Cheap fingerprint of the station table: changes when rows, prices or coordinates do."""
    from django.db.models import Count, Sum

    from routing.models import FuelStation

    agg = FuelStation.objects.aggregate(n=Count("pk"), price=Sum("retail_price"), lat=Sum("lat"), lon=Sum("lon"))
    if not agg["n"]:
        return None
    return f"{agg['n']}:{agg['price']}:{agg['lat']:.6f}:{agg['lon']:.6f}"


def get_station_index():
    global _index, _checked_at
    if _index is not None and time.monotonic() - _checked_at < VERSION_CHECK_SECONDS:
        return _index
    with _lock:
        if _index is None or time.monotonic() - _checked_at >= VERSION_CHECK_SECONDS:
            from routing.models import FuelStation

            version = _table_version()
            if version is None:
                raise StationsUnavailable("No fuel stations loaded. Run `python manage.py load_stations`.")
            if _index is None or _index.version != version:
                rows = list(FuelStation.objects.values_list("opis_id", "lat", "lon", "retail_price"))
                ids, lats, lons, prices = zip(*rows)
                _index = StationIndex(ids, lats, lons, [float(p) for p in prices], version)
            _checked_at = time.monotonic()
    return _index


def reset_station_index():
    global _index
    with _lock:
        _index = None
