"""Turn a user's location string into (lat, lon) - usually with zero network calls.

Accepted inputs:
  * "41.8781,-87.6298"           -> parsed directly
  * "Chicago, IL" / "Chicago, Illinois" / "Chicago, IL, USA" -> looked up in
    the bundled Census Gazetteer index (48k US places), no network
  * anything else ("1600 Pennsylvania Ave, Washington DC") -> one Nominatim
    call, cached. So is a city whose Gazetteer point is flagged unreliable
    (mostly-water or very large places such as San Francisco).
"""

import csv
import hashlib
import re
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from django.conf import settings
from django.core.cache import cache

from routing import nominatim
from routing.text import STATE_CODES, STATE_NAMES, lookup_keys

from .geo import haversine_miles

# Coarse box around the US incl. Alaska and Hawaii, then a finer check: the
# point must lie within US_MAX_MILES of some US place. Every point of the
# Gazetteer is within ~35 mi of another; the ocean, the Caribbean and most of
# Mexico and Canada are not. Border cities (Tijuana, Windsor, Toronto) still
# pass - OSRM then routes through them, which is a documented limitation.
US_LAT = (18.0, 72.0)
US_LON = (-180.0, -66.0)
US_MAX_MILES = 40.0
NOT_FOUND_CACHE_SECONDS = 10 * 60

_LATLON = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")
_COUNTRY = {"USA", "US", "UNITED STATES", "UNITED STATES OF AMERICA"}


class GeocodingError(Exception):
    """The location is invalid, not found or outside the US (client error)."""


class GeocodingUnavailable(GeocodingError):
    """The geocoding service could not be reached (upstream error)."""


@dataclass
class Location:
    query: str
    lat: float
    lon: float
    label: str
    source: str  # "coordinates" | "gazetteer" | "nominatim"


@dataclass
class _Places:
    index: dict  # (name, state) -> (lat, lon, reliable)
    lats: np.ndarray  # every place, for the US check
    lons: np.ndarray


_places = None
_lock = threading.Lock()


def _place_data():
    global _places
    if _places is None:
        with _lock:
            if _places is None:
                path = Path(settings.DATA_DIR) / "us_places.csv"
                with open(path, encoding="utf-8") as fh:
                    index = {
                        (r["name"], r["state"]): (float(r["lat"]), float(r["lon"]), r.get("point") != "unreliable")
                        for r in csv.DictReader(fh)
                    }
                pts = np.array([v[:2] for v in index.values()])
                _places = _Places(index, pts[:, 0], pts[:, 1])
    return _places


def _in_us(lat, lon):
    if not (US_LAT[0] <= lat <= US_LAT[1] and US_LON[0] <= lon <= US_LON[1]):
        return False
    places = _place_data()
    return haversine_miles(lat, lon, places.lats, places.lons).min() <= US_MAX_MILES


def _parse_city_state(text):
    """("Chicago", "IL") for exactly "City, State" (optionally ", USA"); None for anything longer."""
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) == 3 and parts[-1].upper().replace(".", "") in _COUNTRY:
        parts = parts[:2]
    if len(parts) != 2:
        return None  # a street address must not collapse to its city's centre
    raw = parts[1].upper().replace(".", "")
    state = raw if raw in STATE_CODES else STATE_NAMES.get(parts[1].lower())
    return (parts[0], state) if state else None


def geocode(text, calls=None):
    text = (text or "").strip()
    if not text:
        raise GeocodingError("Location is required.")

    m = _LATLON.match(text)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if not _in_us(lat, lon):
            raise GeocodingError(f"'{text}' is outside the USA (expected 'lat,lon').")
        return Location(text, lat, lon, f"{lat:.5f},{lon:.5f}", "coordinates")

    parsed = _parse_city_state(text)
    if parsed:
        city, state = parsed
        index = _place_data().index
        hit = next((index[(k, state)] for k in lookup_keys(city) if (k, state) in index), None)
        if hit:
            gazetteer = Location(text, hit[0], hit[1], f"{city.title()}, {state}", "gazetteer")
            if hit[2]:
                return gazetteer
            try:  # the Census point of this place may be far from its centre
                return _nominatim(text, calls)
            except GeocodingError:
                return gazetteer

    return _nominatim(text, calls)


def _nominatim(text, calls):
    key = "geo:v2:" + hashlib.sha1(re.sub(r"\s+", " ", text.lower()).encode()).hexdigest()
    hit = cache.get(key)
    if hit == "not-found":
        raise GeocodingError(f"Could not find '{text}' in the USA.")
    if hit:
        return Location(**{**hit, "query": text})
    if calls is not None:
        calls.append("nominatim")
    try:
        result = nominatim.search(
            {"q": text, "countrycodes": "us"},
            user_agent=settings.HTTP_USER_AGENT,
            url=settings.NOMINATIM_URL,
            timeout=settings.EXTERNAL_TIMEOUT_SECONDS,
        )
    except nominatim.NominatimUnavailable as exc:
        raise GeocodingUnavailable(f"Geocoding service unavailable: {exc}") from exc
    if result is None or not _in_us(result[0], result[1]):
        cache.set(key, "not-found", NOT_FOUND_CACHE_SECONDS)
        raise GeocodingError(f"Could not find '{text}' in the USA.")
    loc = Location(text, result[0], result[1], result[2] or text, "nominatim")
    cache.set(key, loc.__dict__, settings.ROUTE_CACHE_SECONDS)
    return loc
