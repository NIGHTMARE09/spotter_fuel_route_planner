from decimal import Decimal
from unittest import mock
from urllib.parse import parse_qs, urlparse

import numpy as np
import pytest
import requests
from django.core.cache import cache
from rest_framework.test import APIClient

from routing.models import FuelStation
from routing.services.geo import haversine_miles
from routing.services.stations import reset_station_index

# Fake OSRM: a straight road along latitude 40 between the requested longitudes.
ROUTE_MILES = float(haversine_miles(40, -100, 40, -88))  # ~634 mi for the default trip


def osrm_ok(url, *args, **kwargs):
    (lon1, _), (lon2, _) = (map(float, p.split(",")) for p in url.rsplit("/", 1)[1].split(";"))
    coords = [[float(x), 40.0] for x in np.linspace(lon1, lon2, 25)]
    resp = mock.Mock(status_code=200)
    resp.json.return_value = {
        "code": "Ok",
        "routes": [{
            "distance": float(haversine_miles(40, lon1, 40, lon2)) * 1609.344,
            "duration": 36000,
            "geometry": {"coordinates": coords},
        }],
    }
    return resp


@pytest.fixture(autouse=True)
def clean_state(settings):
    settings.OSRM_RETRY_BACKOFF_SECONDS = 0
    cache.clear()
    reset_station_index()
    yield
    reset_station_index()


@pytest.fixture
def stations(db):
    rows = [
        (1, "PRICEY START", 40.0, -99.0, "4.00"),
        (2, "CHEAP MIDDLE", 40.0, -95.0, "3.00"),
        (3, "LATE", 40.0, -91.0, "3.50"),
        (4, "FAR OFF ROUTE", 41.5, -95.0, "1.00"),  # ~100 mi north: must be ignored
    ]
    FuelStation.objects.bulk_create(
        FuelStation(opis_id=i, name=n, address="I-80", city="X", state="NE", lat=la, lon=lo, retail_price=Decimal(p))
        for i, n, la, lo, p in rows
    )


@pytest.fixture
def client():
    return APIClient()


def test_plans_cheapest_stop_with_one_external_call(client, stations):
    with mock.patch("routing.services.osrm.requests.get", side_effect=osrm_ok) as get:
        r = client.get("/api/route/", {"start": "40,-100", "finish": "40,-88"})
    assert r.status_code == 200, r.content
    body = r.json()

    # Full tank covers 500 mi; the remaining ~134 mi is bought at the $3.00 station,
    # not the $4.00 one we pass first.
    assert [s["name"] for s in body["fuel_stops"]] == ["CHEAP MIDDLE"]
    expected = (ROUTE_MILES - 500) / 10 * 3.0
    assert body["summary"]["total_fuel_cost"] == pytest.approx(expected, abs=0.02)
    assert body["meta"]["external_api_calls"] == 1
    assert get.call_count == 1
    assert body["route"]["geometry"]["type"] == "LineString"
    assert "map_url" in body


def test_repeat_request_is_served_from_cache(client, stations):
    with mock.patch("routing.services.osrm.requests.get", side_effect=osrm_ok) as get:
        client.get("/api/route/", {"start": "40,-100", "finish": "40,-88"})
        r = client.get("/api/route/", {"start": "40,-100", "finish": "40,-88"})
    assert get.call_count == 1
    assert r.json()["meta"] == {**r.json()["meta"], "cache_hit": True, "external_api_calls": 0, "external_api_detail": []}


def test_empty_tank_buys_at_cheapest_reachable_points(client, stations):
    with mock.patch("routing.services.osrm.requests.get", side_effect=osrm_ok):
        r = client.post("/api/route/", {"start": "40,-99", "finish": "40,-88", "start_fuel_gallons": 0}, format="json")
    # Starting on top of the $4 station with nothing in the tank: buy only enough
    # to reach the $3 station, fill there, and cruise past the $3.50 one.
    body = r.json()
    assert r.status_code == 200, body
    assert [s["name"] for s in body["fuel_stops"]] == ["PRICEY START", "CHEAP MIDDLE"]


def test_city_names_resolve_locally_without_geocoding_calls(client, stations):
    with mock.patch("routing.services.osrm.requests.get", side_effect=osrm_ok) as get:
        # Fake road runs along lat 40; fixture stations keep every gap under 500 mi.
        r = client.get("/api/route/", {"start": "Chicago, IL", "finish": "Denver, Colorado"})
    body = r.json()
    assert r.status_code == 200, body
    assert body["start"]["resolved_by"] == "gazetteer"
    assert body["finish"]["resolved_by"] == "gazetteer"
    assert get.call_count == 1  # only OSRM


@pytest.mark.parametrize(
    "params,code",
    [
        ({"start": "40,-100"}, 400),  # missing finish
        ({"start": "51.5,-0.12", "finish": "40,-88"}, 400),  # London
        ({"start": "40,-100", "finish": "40,-88", "start_fuel_gallons": 80}, 400),  # > tank
    ],
)
def test_bad_input(client, stations, params, code):
    with mock.patch("routing.services.osrm.requests.get", side_effect=osrm_ok):
        assert client.get("/api/route/", params).status_code == code


def test_no_station_in_range_is_422(client, db):
    FuelStation.objects.create(opis_id=9, name="ONLY", address="", city="X", state="NE",
                               lat=40.0, lon=-89.0, retail_price=Decimal("3"))
    with mock.patch("routing.services.osrm.requests.get", side_effect=osrm_ok):
        r = client.get("/api/route/", {"start": "40,-100", "finish": "40,-88", "start_fuel_gallons": 5})
    assert r.status_code == 422
    assert r.json()["error"] == "no_feasible_plan"


def test_no_road_route_is_422(client, stations):
    bad = mock.Mock(status_code=400)
    bad.json.return_value = {"code": "NoRoute", "message": "Impossible route between points"}
    with mock.patch("routing.services.osrm.requests.get", return_value=bad):
        r = client.get("/api/route/", {"start": "40,-100", "finish": "40,-88"})
    assert r.status_code == 422 and r.json()["error"] == "no_route"


def test_routing_service_error_is_502(client, stations):
    bad = mock.Mock(status_code=400)
    bad.json.return_value = {"code": "InvalidQuery", "message": "Query string malformed"}
    with mock.patch("routing.services.osrm.requests.get", return_value=bad):
        r = client.get("/api/route/", {"start": "40,-100", "finish": "40,-88"})
    assert r.status_code == 502 and r.json()["error"] == "routing_failed"


def test_long_retry_after_is_not_waited_for(client, stations):
    busy = mock.Mock(status_code=429, headers={"Retry-After": "30"})
    with mock.patch("routing.services.osrm.requests.get", return_value=busy) as get:
        r = client.get("/api/route/", {"start": "40,-100", "finish": "40,-88"})
    assert r.status_code == 502 and get.call_count == 1


def test_geocoder_down_is_503_not_a_client_error(client, stations):
    with mock.patch("routing.nominatim.requests.get", side_effect=requests.ConnectionError("down")):
        r = client.get("/api/route/", {"start": "1600 Pennsylvania Ave NW, Washington, DC", "finish": "40,-88"})
    assert r.status_code == 503 and r.json()["error"] == "geocoding_unavailable"


def test_place_not_found_is_400_and_cached(client, stations):
    empty = mock.Mock(status_code=200)
    empty.json.return_value = []
    with mock.patch("routing.nominatim.requests.get", return_value=empty) as get:
        for _ in range(2):
            r = client.get("/api/route/", {"start": "Nowhereville Xyzzy", "finish": "40,-88"})
            assert r.status_code == 400 and r.json()["error"] == "geocoding_failed"
    assert get.call_count == 1  # negative result cached


def test_no_stations_loaded_is_503(client, db):
    r = client.get("/api/route/", {"start": "40,-100", "finish": "40,-88"})
    assert r.status_code == 503 and r.json()["error"] == "stations_not_loaded"


def test_map_url_carries_every_planning_parameter(client, stations):
    params = {"start": "40,-100", "finish": "40,-88", "start_fuel_gallons": 20, "corridor_miles": 25, "stop_cost": 0}
    with mock.patch("routing.services.osrm.requests.get", side_effect=osrm_ok):
        body = client.get("/api/route/", params).json()
    q = {k: v[0] for k, v in parse_qs(urlparse(body["map_url"]).query).items()}
    assert q == {"start": "40,-100", "finish": "40,-88", "start_fuel_gallons": "20.0", "corridor_miles": "25.0",
                 "stop_cost": "0.0", "reserve_miles": "0.0"}


def test_post_without_trailing_slash(client, stations):
    with mock.patch("routing.services.osrm.requests.get", side_effect=osrm_ok):
        r = client.post("/api/route", {"start": "40,-100", "finish": "40,-88"}, format="json")
    assert r.status_code == 200


def test_reserve_above_starting_fuel_is_400(client, stations):
    r = client.get("/api/route/", {"start": "40,-100", "finish": "40,-88", "start_fuel_gallons": 2, "reserve_miles": 50})
    assert r.status_code == 400 and "reserve_miles" in r.json()


def test_reserve_is_kept_on_every_arrival(client, stations):
    with mock.patch("routing.services.osrm.requests.get", side_effect=osrm_ok):
        r = client.get("/api/route/", {"start": "40,-100", "finish": "40,-88", "reserve_miles": 30})
    body = r.json()
    assert r.status_code == 200, body
    assert all(s["fuel_on_arrival_gallons"] >= 3 - 0.01 for s in body["fuel_stops"])


def test_totals_are_sums_of_rounded_stops(client, stations):
    with mock.patch("routing.services.osrm.requests.get", side_effect=osrm_ok):
        body = client.post("/api/route/", {"start": "40,-99", "finish": "40,-88", "start_fuel_gallons": 0},
                           format="json").json()
    assert body["summary"]["total_fuel_cost"] == round(sum(s["cost"] for s in body["fuel_stops"]), 2)


def test_stations_reload_after_table_changes(client, stations, monkeypatch):
    from routing.services import stations as st

    monkeypatch.setattr(st, "VERSION_CHECK_SECONDS", 0)
    with mock.patch("routing.services.osrm.requests.get", side_effect=osrm_ok):
        first = client.get("/api/route/", {"start": "40,-100", "finish": "40,-88"}).json()
        FuelStation.objects.filter(opis_id=2).update(retail_price=Decimal("2.00"))
        second = client.get("/api/route/", {"start": "40,-100", "finish": "40,-88"}).json()
    assert second["meta"]["cache_hit"] is False  # new data, new cache key
    assert second["summary"]["total_fuel_cost"] < first["summary"]["total_fuel_cost"]


def test_map_page_renders(client):
    r = client.get("/map/?start=Chicago, IL&finish=Denver, CO")
    assert r.status_code == 200 and b"leaflet" in r.content


def test_osrm_is_retried_once_and_every_attempt_is_counted(client, stations):
    flaky = [requests.ConnectionError("reset"), None]

    def first_fails(url, *args, **kwargs):
        err = flaky.pop(0)
        if err:
            raise err
        return osrm_ok(url)

    with mock.patch("routing.services.osrm.requests.get", side_effect=first_fails):
        r = client.get("/api/route/", {"start": "40,-100", "finish": "40,-88"})
    assert r.status_code == 200
    assert r.json()["meta"]["external_api_calls"] == 2


def test_osrm_overloaded_twice_is_502(client, stations):
    busy = mock.Mock(status_code=429)
    with mock.patch("routing.services.osrm.requests.get", return_value=busy) as get:
        r = client.get("/api/route/", {"start": "40,-100", "finish": "40,-88"})
    assert r.status_code == 502 and get.call_count == 2


def test_short_trip_on_full_tank_still_reports_trip_fuel_cost(client, stations):
    with mock.patch("routing.services.osrm.requests.get", side_effect=osrm_ok):
        r = client.get("/api/route/", {"start": "40,-100", "finish": "40,-94"})
    s = r.json()["summary"]
    gallons = float(haversine_miles(40, -100, 40, -94)) / 10
    assert s["total_fuel_cost"] == 0 and s["number_of_stops"] == 0
    # starting fuel valued at the cheapest station within the first tank ($3.00 at lon -95),
    # not the first one passed ($4.00), so a full tank never looks dearer than an empty one
    assert s["starting_fuel_valued_at"] == 3.0
    assert s["trip_fuel_cost"] == pytest.approx(gallons * 3.0, abs=0.02)


def test_trip_fuel_cost_includes_pump_spend(client, stations):
    with mock.patch("routing.services.osrm.requests.get", side_effect=osrm_ok):
        s = client.get("/api/route/", {"start": "40,-100", "finish": "40,-88"}).json()["summary"]
    used = s["trip_gallons_consumed"] - s["total_gallons_purchased"]
    assert s["starting_fuel_used_gallons"] == pytest.approx(used, abs=0.01)
    assert s["trip_fuel_cost"] == pytest.approx(s["total_fuel_cost"] + used * 3.0, abs=0.05)
