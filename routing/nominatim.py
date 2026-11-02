"""Minimal Nominatim client shared by the API and the offline scripts (no Django imports).

OSMF usage policy: an absolute maximum of 1 request per second per application,
an identifying User-Agent, and caching of results. Calls are serialised through
one lock so concurrent API requests in a process never exceed that rate.
"""

import threading
import time

import requests

DEFAULT_URL = "https://nominatim.openstreetmap.org"
USER_AGENT = "spotter-fuel-route/1.0 (assessment)"
MIN_INTERVAL_SECONDS = 1.1  # tests set this to 0

_lock = threading.Lock()
_last_call = 0.0


class NominatimUnavailable(Exception):
    """Network error, timeout, rate limit or a non-JSON answer."""


def _get(url, params, user_agent, timeout):
    global _last_call
    with _lock:
        wait = _last_call + MIN_INTERVAL_SECONDS - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            resp = requests.get(url, params={**params, "format": "json"}, headers={"User-Agent": user_agent}, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise NominatimUnavailable(str(exc)) from exc
        finally:
            _last_call = time.monotonic()


def search(params, *, user_agent=USER_AGENT, url=DEFAULT_URL, timeout=10):
    """First hit for a /search query as (lat, lon, display_name, country_code), or None if nothing matched."""
    hits = _get(f"{url}/search", {**params, "limit": 1, "addressdetails": 1}, user_agent, timeout)
    if not hits:
        return None
    hit = hits[0]
    country = (hit.get("address") or {}).get("country_code")
    return float(hit["lat"]), float(hit["lon"]), hit.get("display_name", ""), country


def country_code(lat, lon, *, user_agent=USER_AGENT, url=DEFAULT_URL, timeout=10):
    """ISO country code ("us", "ca", ...) at a point, or None at sea."""
    data = _get(f"{url}/reverse", {"lat": lat, "lon": lon, "zoom": 3}, user_agent, timeout)
    return (data.get("address") or {}).get("country_code")
