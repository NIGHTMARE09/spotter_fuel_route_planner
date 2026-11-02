"""One-off: attach coordinates to every truck stop in the fuel-price CSV.

Done offline, once, so the API never geocodes stations at request time.

1. Deduplicate by OPIS id (the CSV repeats ~600 stations with different
   names/prices); keep the lowest listed price.
2. Drop non-US rows (Canadian provinces) - the API only serves US routes.
3. Look up (city, state) in data/us_places.csv (Census Gazetteer).
4. For cities the Gazetteer misses, or whose Gazetteer point is flagged
   unreliable, ask Nominatim (1 req/s, cached in data/nominatim_cache.json so
   re-runs are free). A Nominatim hit is kept only if it lies within
   MAX_HIT_MILES of some Gazetteer place in the station's own state; this
   rejects "Canton, MI" resolving to Canton, Mississippi.

Output: data/stations_geocoded.csv

Usage:
    python scripts/geocode_stations.py [--no-network]
"""

import csv
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from routing import nominatim  # noqa: E402
from routing.text import STATE_CODES, lookup_keys  # noqa: E402

DATA = ROOT / "data"
SRC = DATA / "fuel-prices.csv"
PLACES = DATA / "us_places.csv"
CACHE = DATA / "nominatim_cache.json"
OUT = DATA / "stations_geocoded.csv"

MAX_HIT_MILES = 40.0
EARTH_RADIUS_MILES = 3958.7613


def load_places():
    with open(PLACES, encoding="utf-8") as fh:
        return {
            (r["name"], r["state"]): (float(r["lat"]), float(r["lon"]), r.get("point") != "unreliable")
            for r in csv.DictReader(fh)
        }


class StateCheck:
    """Distance from a point to the nearest Gazetteer place of a given state."""

    def __init__(self, places):
        by_state = {}
        for (_, state), (lat, lon, _) in places.items():
            by_state.setdefault(state, []).append((lat, lon))
        self.points = {s: np.radians(np.array(p)) for s, p in by_state.items()}

    def miles_to_state(self, lat, lon, state):
        pts = self.points.get(state)
        if pts is None:
            return float("inf")
        p, lam = np.radians(lat), np.radians(lon)
        a = np.sin((pts[:, 0] - p) / 2) ** 2 + np.cos(p) * np.cos(pts[:, 0]) * np.sin((pts[:, 1] - lam) / 2) ** 2
        return float(2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(a.min())))


def save_cache(cache):
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=0, sort_keys=True))
    tmp.replace(CACHE)  # atomic: an interrupted run never leaves broken JSON


def nominatim_lookup(city, state, cache, offline, check):
    """Structured query first, then free text; each answer cached, each validated against the state."""
    queries = (
        (f"{city}|{state}", {"city": city, "state": state, "country": "USA"}),
        (f"q:{city}|{state}", {"q": f"{city}, {state}, USA", "countrycodes": "us"}),
    )
    for key, params in queries:
        if key not in cache:
            if offline:
                continue
            try:
                hit = nominatim.search(params)
            except nominatim.NominatimUnavailable as exc:
                print(f"  nominatim error for {city}, {state}: {exc} (not cached; re-run to retry)")
                continue
            cache[key] = [hit[0], hit[1]] if hit else None
            save_cache(cache)
        point = cache[key]
        if point is None:
            continue
        dist = check.miles_to_state(point[0], point[1], state)
        if dist <= MAX_HIT_MILES:
            return point
        print(f"  rejected nominatim hit for {city}, {state}: {dist:.0f} mi from any {state} place")
    return None


def main(offline=False):
    places = load_places()
    check = StateCheck(places)
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}

    stations = {}
    with open(SRC, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            state = r["State"].strip()
            if state not in STATE_CODES:
                continue
            sid = int(r["OPIS Truckstop ID"])
            price = float(r["Retail Price"])
            if sid not in stations or price < stations[sid]["price"]:
                stations[sid] = {
                    "opis_id": sid,
                    "name": r["Truckstop Name"].strip(),
                    "address": r["Address"].strip(),
                    "city": r["City"].strip(),
                    "state": state,
                    "price": price,
                }

    stats = {"gazetteer": 0, "nominatim": 0, "gazetteer-unreliable": 0, "missing": 0}
    out = []
    for s in stations.values():
        gaz = next((places[(k, s["state"])] for k in lookup_keys(s["city"]) if (k, s["state"]) in places), None)
        coord, src = (gaz[:2], "gazetteer") if gaz and gaz[2] else (None, None)
        if coord is None:
            coord, src = nominatim_lookup(s["city"], s["state"], cache, offline, check), "nominatim"
        if coord is None and gaz:
            coord, src = gaz[:2], "gazetteer-unreliable"
        if coord is None:
            stats["missing"] += 1
            print(f"  no coords: {s['city']}, {s['state']} ({s['name']})")
            continue
        stats[src] += 1
        out.append({**s, "lat": round(coord[0], 6), "lon": round(coord[1], 6), "geo_source": src})

    out.sort(key=lambda s: s["opis_id"])
    with open(OUT, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)
    print(f"{len(stations)} unique US stations -> {len(out)} geocoded {stats}")


if __name__ == "__main__":
    main(offline="--no-network" in sys.argv)
