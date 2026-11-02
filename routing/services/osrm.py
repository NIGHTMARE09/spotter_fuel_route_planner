"""Routing via the public OSRM server (free, no API key).

One HTTP call per (start, finish) pair (two if the demo server drops the
first); results are cached so a repeated query costs zero external calls.
"""

import time
from dataclasses import dataclass

import requests
from django.conf import settings
from django.core.cache import cache

METERS_PER_MILE = 1609.344
MAX_RETRY_AFTER_SECONDS = 2.0  # a longer Retry-After is not worth holding the request for
NO_ROUTE_CODES = {"NoRoute", "NoSegment"}


class RoutingError(Exception):
    """The routing service failed (upstream error)."""


class NoRoute(RoutingError):
    """The service works but cannot connect these points by road (e.g. Hawaii -> mainland)."""


@dataclass
class Route:
    distance_miles: float
    duration_seconds: float
    coords: list  # [[lon, lat], ...]


def _cache_key(start, finish):
    (a, b), (c, d) = start, finish
    return f"osrm:v1:{a:.5f},{b:.5f}:{c:.5f},{d:.5f}"


def _fetch(url, calls):
    """GET with one retry: the public OSRM demo server drops connections and returns 429/5xx under load.

    Every attempt is recorded in `calls`, so the reported external-call count stays honest.
    """
    last_error = None
    for attempt in range(1 + settings.OSRM_RETRIES):
        if attempt:
            time.sleep(settings.OSRM_RETRY_BACKOFF_SECONDS * attempt)
        if calls is not None:
            calls.append("osrm")
        try:
            resp = requests.get(
                url,
                params={"overview": "full", "geometries": "geojson", "steps": "false"},
                timeout=settings.OSRM_TIMEOUT,
                headers={"User-Agent": settings.HTTP_USER_AGENT},
            )
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}"
                wait = _retry_after(resp)
                if wait is not None and wait > MAX_RETRY_AFTER_SECONDS:
                    break
                if wait:
                    time.sleep(wait)
                continue
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
    raise RoutingError(f"Routing service unavailable: {last_error}")


def _retry_after(resp):
    try:
        return float(resp.headers.get("Retry-After", ""))
    except (TypeError, ValueError):
        return None  # absent, or an HTTP date: fall back to our own backoff


def get_route(start, finish, calls=None):
    """`start`/`finish` are (lat, lon). `calls` is a counter list for reporting external calls."""
    key = _cache_key(start, finish)
    hit = cache.get(key)
    if hit is not None:
        return Route(**hit)

    url = (
        f"{settings.OSRM_BASE_URL}/route/v1/driving/"
        f"{start[1]:.6f},{start[0]:.6f};{finish[1]:.6f},{finish[0]:.6f}"
    )
    data = _fetch(url, calls)
    if data.get("code") in NO_ROUTE_CODES:
        raise NoRoute(f"No road route between these points ({data.get('code')}).")
    if data.get("code") != "Ok" or not data.get("routes"):
        raise RoutingError(data.get("message") or f"Routing failed ({data.get('code')}).")

    r = data["routes"][0]
    route = Route(
        distance_miles=r["distance"] / METERS_PER_MILE,
        duration_seconds=r["duration"],
        coords=r["geometry"]["coordinates"],
    )
    cache.set(key, route.__dict__, settings.ROUTE_CACHE_SECONDS)
    return route
