from unittest import mock

import numpy as np
import pytest

from routing.services.geo import densify, haversine_miles, simplify
from routing.services.geocoding import GeocodingError, _parse_city_state, geocode
from routing.services.stations import StationIndex
from routing.text import lookup_keys, normalize_place


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Salt Lake City city", "salt lake city"),
        ("Kansas City city", "kansas city"),
        ("Indianapolis city (balance)", "indianapolis"),
        ("Big Cabin town", "big cabin"),
        ("St. Louis city", "saint louis"),
    ],
)
def test_gazetteer_suffixes(raw, expected):
    assert normalize_place(raw, strip_suffix=True) == expected


@pytest.mark.parametrize(
    "text,state",
    [("Chicago, IL", "IL"), ("Salt Lake City, Utah", "UT"), ("New York City, NY", "NY"),
     ("St. Louis, MO", "MO"), ("Nashville, TN", "TN"), ("Winston-Salem, NC", "NC")],
)
def test_city_state_resolves_offline(text, state):
    calls = []
    loc = geocode(text, calls)
    assert loc.source == "gazetteer" and calls == []
    assert loc.label.endswith(state)


def test_coordinates_and_bounds():
    assert geocode("41.88, -87.63").source == "coordinates"
    with pytest.raises(GeocodingError):
        geocode("48.85,2.35")  # Paris


def reverse_says(country):
    resp = mock.Mock(status_code=200)
    resp.json.return_value = {"address": {"country_code": country}} if country else {"error": "Unable to geocode"}
    return mock.patch("routing.nominatim.requests.get", return_value=resp)


@pytest.mark.parametrize(
    "where,country",
    [("25.6866,-100.3161", "mx"), ("23.1136,-82.3666", "cu"), ("45.4215,-75.6972", "ca"), ("27.0,-90.0", None)],
)  # Monterrey, Havana, Ottawa, Gulf of Mexico: far from any US place, so the reverse lookup decides
def test_points_far_from_us_places_are_checked_by_reverse_lookup(where, country):
    with reverse_says(country) as get, pytest.raises(GeocodingError, match="outside the USA"):
        geocode(where)
    assert get.call_count == 1


def test_interior_alaska_is_accepted_by_reverse_lookup():
    calls = []
    with reverse_says("us"):
        assert geocode("64.79,-153.27", calls).source == "coordinates"  # 45 mi from the nearest Census place
    assert calls == ["nominatim"]


@pytest.mark.parametrize("where", ["61.2,-149.9", "21.3,-157.85", "44.0,-103.0", "25.08,-80.45"])  # AK, HI, SD, Keys
def test_us_points_near_places_need_no_network(where):
    calls = []
    assert geocode(where, calls).source == "coordinates" and calls == []


def test_free_text_resolving_abroad_is_rejected_not_forced_into_the_us():
    hit = mock.Mock(status_code=200)
    hit.json.return_value = [{"lat": "43.65", "lon": "-79.38", "display_name": "Toronto, Ontario, Canada",
                              "address": {"country_code": "ca"}}]
    with mock.patch("routing.nominatim.requests.get", return_value=hit) as get:
        with pytest.raises(GeocodingError, match="Toronto, Ontario, Canada, outside the USA"):
            geocode("Toronto, ON")
    assert "countrycodes" not in get.call_args.kwargs["params"]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Chicago, IL", ("Chicago", "IL")),
        ("Chicago, Illinois, USA", ("Chicago", "IL")),
        ("350 5th Ave, New York, NY", None),  # an address must not collapse to the city centre
        ("Paris, France", None),
    ],
)
def test_only_plain_city_state_uses_the_gazetteer(text, expected):
    assert _parse_city_state(text) == expected


@pytest.mark.parametrize(
    "name,keys",
    [
        ("Greenwich Village", ["greenwich village", "greenwichvillage", "greenwich village city"]),
        ("La Place", ["la place", "laplace", "la place city"]),
        ("New York City", ["new york city", "newyorkcity", "new york"]),
        ("Cañon City", ["canon city", "canoncity", "canon"]),
    ],
)
def test_lookup_keys_are_spelling_variants_only(name, keys):
    assert lookup_keys(name) == keys


def test_station_cities_resolve_to_their_own_state():
    # Census suffixes "charter township" / CDP spellings used to miss and fall to a wrong-state Nominatim hit.
    canton = geocode("Canton, MI")
    assert canton.source == "gazetteer" and haversine_miles(canton.lat, canton.lon, 42.31, -83.48) < 10
    laplace = geocode("LaPlace, LA")
    assert laplace.source == "gazetteer" and haversine_miles(laplace.lat, laplace.lon, 30.07, -90.48) < 10


def test_densify_keeps_length_and_spacing():
    lat, lon, cum = densify([[-100, 40], [-90, 40]], step_miles=1)
    assert cum[-1] == pytest.approx(float(haversine_miles(40, -100, 40, -90)))
    assert np.diff(cum).max() <= 1.0 + 1e-9


def test_simplify_drops_collinear_points():
    line = [[x, 40.0] for x in np.linspace(-100, -90, 1000)]
    assert len(simplify(line)) == 2


def test_station_index_corridor_and_mile_markers():
    idx = StationIndex(ids=[1, 2, 3], lats=[40.0, 40.05, 41.0], lons=[-99.0, -95.0, -95.0], prices=[3, 3, 3])
    hits = idx.along_route([[-100, 40], [-90, 40]], corridor_miles=10)
    assert [h.station_id for h in hits] == [1, 2]  # #3 is ~69 mi north
    assert hits[0].mile == pytest.approx(float(haversine_miles(40, -100, 40, -99)), abs=0.5)
    assert hits[1].off_route_miles == pytest.approx(3.45, abs=0.2)


def test_colocated_stations_collapse_to_cheapest():
    # Three stations geocoded to one city centroid, plus one elsewhere on the route.
    idx = StationIndex(
        ids=[10, 11, 12, 20],
        lats=[40.0, 40.0, 40.0, 40.0],
        lons=[-97.0, -97.0, -97.0, -93.0],
        prices=[3.50, 3.10, 3.80, 3.30],
    )
    hits = idx.along_route([[-100, 40], [-90, 40]], corridor_miles=10)
    assert [(h.station_id, h.price) for h in hits] == [(11, 3.10), (20, 3.30)]
    assert hits[0].mile < hits[1].mile
