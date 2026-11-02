"""One-off: build data/us_places.csv from the US Census Gazetteer files.

The Gazetteer (public domain) gives an internal point (lat/lon) for every
incorporated place, CDP and county subdivision in the US. We strip the
legal suffix ("city", "town", "CDP", ...) so names match how humans and
the fuel-price CSV write them, e.g. "Big Cabin town" -> "big cabin".

The internal point is a point *inside the legal area*, not the downtown. For
areas that are mostly water or very large it can be far off: San Francisco's
lies in the Pacific near the Farallones. Those rows are flagged, and with
--fix-points each flagged incorporated place/CDP is re-pointed with one
Nominatim lookup (1 req/s, cached in data/place_points_cache.json). Anything
still unreliable is written with point=unreliable so the API asks Nominatim.

Usage:
    python scripts/build_places.py <2024_Gaz_place_national.txt> <2024_Gaz_cousubs_national.txt> [--fix-points]
"""

import csv
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from routing import nominatim  # noqa: E402
from routing.text import normalize_place, strip_census_suffix  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data"
OUT = DATA / "us_places.csv"
POINT_CACHE = DATA / "place_points_cache.json"

UNRELIABLE_WATER_SHARE = 0.3  # internal point may sit in the water
UNRELIABLE_LAND_M2 = 1_000_000_000  # >1,000 km2: internal point may be far from the centre


def read_gazetteer(path, source):
    with open(path, encoding="utf-8") as fh:
        reader = csv.reader(fh, delimiter="\t")
        header = [h.strip() for h in next(reader)]
        idx = {h: i for i, h in enumerate(header)}
        for row in reader:
            row = [c.strip() for c in row]
            raw = row[idx["NAME"]]
            aland, awater = int(row[idx["ALAND"]]), int(row[idx["AWATER"]])
            rec = {
                "state": row[idx["USPS"]],
                "lat": float(row[idx["INTPTLAT"]]),
                "lon": float(row[idx["INTPTLONG"]]),
                # Prefer populated places: bigger land area ~ the "main" one.
                "aland": aland,
                "area_sqmi": (aland + awater) / 2_589_988,
                "unreliable": awater / max(1, aland + awater) > UNRELIABLE_WATER_SHARE or aland > UNRELIABLE_LAND_M2,
                "display": strip_census_suffix(raw),
                "source": source,
            }
            name = normalize_place(raw, strip_suffix=True)
            if name:
                yield {**rec, "name": name}
            # Consolidated city-counties: "Nashville-Davidson metropolitan government
            # (balance)" / "Macon-Bibb County" should also answer to "Nashville" / "Macon",
            # and the CDP "Urban Honolulu" to "Honolulu".
            # Aliases rank below real places, so they never shadow one.
            if "-" in raw or "/" in raw:
                short = normalize_place(re.split(r"[-/]", raw)[0])
            elif raw.startswith("Urban "):
                short = normalize_place(raw[len("Urban "):], strip_suffix=True)
            else:
                short = None
            if short:
                yield {**rec, "name": short, "source": source + "-alias"}


def miles_between(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 3958.7613 * math.asin(math.sqrt(a))


def fix_point(rec, cache):
    """Nominatim's point for an unreliable place, if it lands plausibly inside that place."""
    key = f"{rec['display']}|{rec['state']}"
    if key not in cache:
        try:
            hit = nominatim.search(
                {"city": rec["display"], "state": rec["state"], "country": "USA"}, user_agent=nominatim.USER_AGENT
            )
        except nominatim.NominatimUnavailable as exc:
            print(f"  nominatim error for {key}: {exc} (not cached)")
            return None
        cache[key] = [hit[0], hit[1]] if hit else None
        tmp = POINT_CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, indent=0, sort_keys=True))
        tmp.replace(POINT_CACHE)  # atomic: an interrupted run never leaves broken JSON
    point = cache[key]
    if point is None:
        return None
    # A same-named place elsewhere in the state would be far from this one's own area.
    if miles_between(rec["lat"], rec["lon"], *point) > max(25.0, 3 * math.sqrt(rec["area_sqmi"])):
        return None
    return point


def main(place_file, cousub_file, fix_points=False):
    rank = {"place": 0, "place-alias": 1, "cousub": 2, "cousub-alias": 3}
    best = {}
    # Real places beat aliases beat county subdivisions; within a tier, larger land area wins.
    for source, path in (("place", place_file), ("cousub", cousub_file)):
        for rec in read_gazetteer(path, source):
            key = (rec["name"], rec["state"])
            cur = best.get(key)
            if cur is None or (rank[rec["source"]], -rec["aland"]) < (rank[cur["source"]], -cur["aland"]):
                best[key] = rec

    cache = json.loads(POINT_CACHE.read_text()) if POINT_CACHE.exists() else {}
    stats = {"census": 0, "nominatim": 0, "unreliable": 0}
    for rec in best.values():
        rec["point"] = "census"
        if rec["unreliable"]:
            fixed = fix_point(rec, cache) if fix_points and rec["source"].startswith("place") else None
            if fixed:
                rec["lat"], rec["lon"], rec["point"] = fixed[0], fixed[1], "nominatim"
            else:
                rec["point"] = "unreliable"
        stats[rec["point"]] += 1

    DATA.mkdir(exist_ok=True)
    with open(OUT, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "state", "lat", "lon", "point"])
        for (name, state), rec in sorted(best.items()):
            w.writerow([name, state, f"{rec['lat']:.6f}", f"{rec['lon']:.6f}", rec["point"]])
    print(f"wrote {len(best)} places -> {OUT} {stats}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    main(args[0], args[1], fix_points="--fix-points" in sys.argv)
