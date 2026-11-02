"""Glue: geocode -> route (1 external call) -> stations on route -> optimal stops."""

import hashlib
import json
import time

from django.conf import settings
from django.core.cache import cache

from routing.models import FuelStation

from .geo import simplify
from .geocoding import geocode
from .optimizer import Station, plan_fuel_stops_dp
from .osrm import get_route
from .stations import get_station_index


def plan_trip(start, finish, *, start_fuel_gallons, corridor_miles, stop_cost, reserve_miles, full_geometry=False):
    t0 = time.perf_counter()
    index = get_station_index()  # raises StationsUnavailable before any external call
    params = {
        "start": start.strip().lower(),
        "finish": finish.strip().lower(),
        "start_fuel_gallons": start_fuel_gallons,
        "corridor_miles": corridor_miles,
        "stop_cost": stop_cost,
        "reserve_miles": reserve_miles,
        "full_geometry": full_geometry,
        "stations": index.version,  # reloading stations invalidates cached plans
    }
    key = "plan:v2:" + hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()
    cached = cache.get(key)
    if cached is not None:
        cached["meta"] = {**cached["meta"], "cache_hit": True, "external_api_calls": 0, "external_api_detail": [],
                          "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)}
        return cached

    calls = []
    origin = geocode(start, calls)
    dest = geocode(finish, calls)
    route = get_route((origin.lat, origin.lon), (dest.lat, dest.lon), calls)

    mpg = settings.VEHICLE_MPG
    max_range = settings.VEHICLE_MAX_RANGE_MILES
    on_route = index.along_route(route.coords, corridor_miles)
    plan = plan_fuel_stops_dp(
        [Station(s.mile, s.price, s) for s in on_route],
        route.distance_miles,
        max_range=max_range,
        mpg=mpg,
        start_fuel_miles=start_fuel_gallons * mpg,
        stop_cost=stop_cost,
        reserve_miles=reserve_miles,
    )

    # One query for the details of the handful of stations we actually stop at.
    details = FuelStation.objects.in_bulk([s.station.ref.station_id for s in plan.stops])
    stops = []
    for n, s in enumerate(plan.stops, 1):
        st, info = details[s.station.ref.station_id], s.station.ref
        stops.append({
            "stop_number": n,
            "opis_id": st.opis_id,
            "name": st.name,
            "address": st.address,
            "city": st.city,
            "state": st.state,
            "location": {"lat": st.lat, "lon": st.lon},
            "mile_marker": round(info.mile, 1),
            "off_route_miles": round(info.off_route_miles, 1),
            "price_per_gallon": round(info.price, 3),
            "fuel_on_arrival_gallons": round(s.fuel_on_arrival_gallons, 2),
            "gallons_purchased": round(s.gallons, 2),
            "cost": round(s.cost, 2),
        })

    # Totals are sums of the rounded stop costs, so the response adds up.
    pump_cost = round(sum(s["cost"] for s in stops), 2)

    # The trip also burns the fuel that was already in the tank. Value it at the
    # cheapest station within the first tank of the route - where the optimizer
    # would have bought it - so a full start tank never looks dearer than an
    # empty one, and a 300-mile trip on a full tank still reports what it cost.
    trip_gallons = route.distance_miles / mpg
    starting_fuel_used = max(0.0, trip_gallons - plan.total_gallons)
    first_tank = [s.price for s in on_route if s.mile <= max_range] or [s.price for s in on_route]
    starting_price = min(first_tank) if first_tank else None
    trip_cost = pump_cost + starting_fuel_used * starting_price if starting_price is not None else None

    geometry = route.coords if full_geometry else simplify(route.coords)
    result = {
        "start": _loc(origin),
        "finish": _loc(dest),
        "route": {
            "distance_miles": round(route.distance_miles, 1),
            "duration_hours": round(route.duration_seconds / 3600, 2),
            "geometry": {"type": "LineString", "coordinates": geometry},
        },
        "fuel_stops": stops,
        "summary": {
            "total_fuel_cost": pump_cost,  # money spent at the pump on this trip
            "total_gallons_purchased": round(plan.total_gallons, 2),
            "trip_fuel_cost": round(trip_cost, 2) if trip_cost is not None else None,
            "trip_gallons_consumed": round(trip_gallons, 2),
            "starting_fuel_used_gallons": round(starting_fuel_used, 2),
            "starting_fuel_valued_at": round(starting_price, 3) if starting_price is not None else None,
            "number_of_stops": len(stops),
            "average_price_paid": round(plan.total_cost / plan.total_gallons, 3) if plan.total_gallons else None,
            # cheapest station per route point inside the corridor - the optimizer's input
            "stations_in_corridor": len(on_route),
        },
        "assumptions": {
            "mpg": mpg,
            "max_range_miles": max_range,
            "tank_gallons": max_range / mpg,
            "start_fuel_gallons": start_fuel_gallons,
            "corridor_miles": corridor_miles,
            "stop_cost": stop_cost,
            "reserve_miles": reserve_miles,
        },
        "meta": {
            "cache_hit": False,
            "external_api_calls": len(calls),
            "external_api_detail": calls,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        },
    }
    cache.set(key, result, settings.ROUTE_CACHE_SECONDS)
    return result


def _loc(loc):
    return {"query": loc.query, "label": loc.label, "lat": loc.lat, "lon": loc.lon, "resolved_by": loc.source}
