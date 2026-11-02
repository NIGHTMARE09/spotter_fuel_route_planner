# Spotter fuel route planner

A Django API that takes a start and a finish in the USA. It returns:

- the driving route and a map link;
- where to buy fuel along the way so the trip costs as little as possible;
- what the fuel for the trip costs.

The vehicle has a 500-mile range and does 10 miles per gallon. Prices come from the provided OPIS list (`data/fuel-prices.csv`).

- **Routing:** the free [OSRM](https://project-osrm.org/) demo server, called **once per trip**. A repeated trip is served from cache with zero external calls.
- **City names:** "City, ST" resolves offline from the US Census Gazetteer.
- **Other text** (addresses, landmarks): one [Nominatim](https://nominatim.org/) call.
- **Speed:** a coast-to-coast plan takes about 1.5 s, almost all of it the OSRM call. Planning itself takes a few ms.

## Quick start

Needs Python 3.12+ (developed on 3.13).

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows; on macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py load_stations
python manage.py runserver
```

- `load_stations` loads the 6,626 pre-geocoded stations from `data/stations_geocoded.csv` into SQLite.
- Open the map at http://127.0.0.1:8000/map/?start=Chicago,%20IL&finish=Houston,%20TX
- Import `postman/fuel-route.postman_collection.json` into Postman for ready-made requests. Every request in it carries tests: the range is respected and the totals add up.

```bash
pytest   # 876 tests, no network needed
```

## API

`GET /api/route/?start=…&finish=…` or `POST /api/route/` with a JSON body.

| Parameter | Default | Meaning |
|---|---|---|
| `start`, `finish` | required | `"Chicago, IL"`, `"Chicago, Illinois"`, an address, or `"41.88,-87.63"` |
| `start_fuel_gallons` | 50 (full tank) | Fuel in the tank at departure, 0–50 |
| `corridor_miles` | 10 | How far off the route a station may be, 1–50 |
| `stop_cost` | 0 | Dollars a stop is worth avoiding. 0 = cheapest fuel, however many stops. 5 = trade a few dollars of fuel for far fewer stops |
| `reserve_miles` | 0 | Fuel (in miles) the plan never dips into, 0–250 |
| `full_geometry` | false | Return every route point instead of a simplified line |

Response for Chicago, IL to Houston, TX (lists trimmed):

```json
{
  "start":  {"label": "Chicago, IL", "lat": 41.837045, "lon": -87.684939, "resolved_by": "gazetteer"},
  "finish": {"label": "Houston, TX", "lat": 29.758938, "lon": -95.367697, "resolved_by": "gazetteer"},
  "route": {"distance_miles": 1083.1, "duration_hours": 19.85,
            "geometry": {"type": "LineString", "coordinates": [[-87.68488, 41.83705], "..."]}},
  "fuel_stops": [
    {"stop_number": 1, "name": "HUCKS FOOD & FUEL #379", "city": "Marion", "state": "IL",
     "location": {"lat": 37.734445, "lon": -88.941164}, "mile_marker": 313.2, "off_route_miles": 1.0,
     "price_per_gallon": 2.929, "fuel_on_arrival_gallons": 18.68, "gallons_purchased": 28.88, "cost": 84.59},
    "..."
  ],
  "summary": {
    "trip_fuel_cost": 314.28,
    "trip_gallons_consumed": 108.31,
    "fuel_purchased_cost": 167.84,
    "total_gallons_purchased": 58.31,
    "starting_fuel_used_gallons": 50.0,
    "starting_fuel_valued_at": 2.929,
    "number_of_stops": 5,
    "average_price_paid": 2.878,
    "stations_in_corridor": 81
  },
  "assumptions": {"mpg": 10, "max_range_miles": 500, "tank_gallons": 50.0, "start_fuel_gallons": 50.0,
                  "corridor_miles": 10.0, "stop_cost": 0.0, "reserve_miles": 0.0},
  "meta": {"cache_hit": false, "external_api_calls": 1, "external_api_detail": ["osrm"], "elapsed_ms": 1400.7},
  "map_url": "http://localhost/map/?start=Chicago%2C+IL&finish=Houston%2C+TX&..."
}
```

**What the money fields mean.**

- `trip_fuel_cost` is the headline. It is the cost of all fuel the trip burns, which is distance ÷ 10 mpg:
  - fuel bought at the stops, at the prices paid;
  - plus the fuel already in the tank at departure, valued at the cheapest station within the first 500 miles (where it would have been bought).

  So the number does not depend on how full the tank was at the start. A 196-mile Dallas→Austin trip on a full tank needs no stop and still reports $54.
- `fuel_purchased_cost` is only what is paid at the listed stops.

**Errors.**

| Status | When |
|---|---|
| 400 | Bad input, a place that cannot be found, or a place outside the USA |
| 422 | No road route (e.g. Hawaii→mainland), or a gap between stations longer than one tank. The message names the gap and suggests a wider `corridor_miles` or more starting fuel |
| 502 / 503 | The routing or geocoding service is down, or stations are not loaded |
| 429 | More than 60 requests a minute from one client |

## How it works

```
start, finish ──► geocode (offline Gazetteer, else Nominatim) ──► OSRM route (1 call, cached)
                                                                      │
       stations near the route (in-memory numpy index) ◄──────────────┘
                      │
                      ▼
       optimizer: cheapest purchases along the route ──► stops, totals, map
```

- **Stations are geocoded offline, once** (`scripts/geocode_stations.py`). The API never geocodes a station at request time.
  - Each station gets its city's Census Gazetteer point.
  - A few cities the Gazetteer misses go to Nominatim. A Nominatim hit is kept only if it lies in the station's own state.
  - Duplicate OPIS ids keep their lowest price.
- **Corridor search.** Stations are unit vectors on the sphere, so "within 10 miles of the route" is a dot product.
  - Pass 1 compares all stations with the route sampled every 5 miles.
  - Pass 2 finds each surviving station's exact nearest route point (its mile marker), in chunks so memory stays around 40 MB on a 3,300-mile route.
  - Stations at the same route point collapse to the cheapest.
- **Optimizer** (`routing/services/optimizer.py`) is a dynamic programme over (station, fuel in the tank, in 0.1-mile steps).
  - It minimises pump cost plus `stop_cost` for each stop, and keeps `reserve_miles` in the tank.
  - One numpy prefix-minimum per station makes it a few milliseconds even with 300 stations.
  - With `stop_cost=0` it reproduces the classic greedy, which is provably optimal for pure fuel cost (Khuller, Malekian & Mestre, *To fill or not to fill*, 2007). The greedy is kept in the code as the reference the tests check against.
  - The tests also compare both against an independent brute-force solver on hundreds of random instances.
- **Respect for the free services.**
  - Nominatim calls are serialised at no more than 1 per second and cached; not-found answers are cached for 10 minutes.
  - OSRM is retried once on a dropped connection or 429/5xx, but a long `Retry-After` is not waited for.
  - Every external call is counted in `meta.external_api_calls`.

## Assumptions and limitations

- **Starting fuel defaults to a full tank.** The truck cannot buy fuel at the origin unless a station is there. With `start_fuel_gallons=0` and no station near the start, the API returns 422 and says so.
- **Station location is the city's point, not the exact pump.** The price list has street-level addresses ("I-57, EXIT 53") but no coordinates. The 10-mile corridor absorbs most of the error.
- **The detour to an off-route station is not counted.** `off_route_miles` reports it.
- **Coverage follows the price list.** California has only 8 stations, all in the Imperial Valley.
  - San Francisco → Los Angeles on a full tank needs no stop.
  - Because no station is in reach, it reports `trip_fuel_cost: null` (there is no price to value the fuel at).
  - On an empty tank it returns 422.
- **Places just across the border can pass the US check.** Coordinates within ~40 miles of a US place (e.g. Windsor, ON) are accepted without a network call. Farther points are checked with Nominatim. Free text that resolves abroad ("Toronto, ON", "Paris") is rejected.
- **Fuel is planned in 0.1-mile steps.** Totals can differ from an exact continuous solution by a few cents.

## Production notes

- **Defaults are for local development.** `DEBUG` is on and `SECRET_KEY` is a placeholder. For production, set `DJANGO_DEBUG=0`, `DJANGO_SECRET_KEY` and `DJANGO_ALLOWED_HOSTS`.
- **Shared cache.** Set `REDIS_URL` to share the route cache and the rate limit across workers. The Nominatim 1-per-second limit is per process.
- **Proxies.** Behind a reverse proxy, set `NUM_PROXIES` so the rate limit sees the real client address.
- **Self-hosted services.** `OSRM_BASE_URL` and `NOMINATIM_URL` can point at your own instances.

## Rebuilding the data (not needed to run)

The repo ships the generated files. To rebuild them:

```bash
# Census Gazetteer 2024 place + county-subdivision files:
# https://www.census.gov/geographies/reference-files/time-series/geo/gazetteer-files.html
python scripts/build_places.py 2024_Gaz_place_national.txt 2024_Gaz_cousubs_national.txt --fix-points
python scripts/geocode_stations.py
python manage.py load_stations
```

`--fix-points` re-points places whose Census point is unreliable (mostly water, or very large: San Francisco's lies in the Pacific). It takes about 14 minutes at Nominatim's rate limit, and results are cached in `data/place_points_cache.json`.
