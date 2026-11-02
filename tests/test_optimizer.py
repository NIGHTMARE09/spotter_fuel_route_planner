import random

import pytest

from routing.services.optimizer import NoFeasiblePlan, Station, plan_fuel_stops, plan_fuel_stops_dp


def dp_min_cost(stations, route_miles, cap, mpg, start_fuel, stop_cost=0.0):
    """Brute-force oracle: DP over (station, integer fuel level).

    Independent of both solvers - it tries every purchase amount at every
    station - so agreement on random instances is real evidence of optimality.
    Returns pump cost + stop_cost per stop.
    """
    nodes = sorted(stations, key=lambda s: s.mile) + [Station(route_miles, 0.0)]
    inf = float("inf")
    best = [inf] * (cap + 1)  # best[f] = min cost standing at the origin with f fuel
    best[start_fuel] = 0.0
    pos, price = 0, None
    for node in nodes:
        # optionally buy any amount at the current spot (not at the origin)
        if price is not None:
            best = [
                min([best[f]] + [best[g] + (f - g) * price / mpg + stop_cost for g in range(f)])
                for f in range(cap + 1)
            ]
        d = node.mile - pos
        nxt = [inf] * (cap + 1)
        for f in range(d, cap + 1):
            nxt[f - d] = best[f]
        best, pos, price = nxt, node.mile, node.price
    return min(best)


def test_buys_only_enough_to_reach_cheaper_station():
    stations = [Station(0, 5.0), Station(100, 3.0)]
    plan = plan_fuel_stops(stations, 400, max_range=500, mpg=10, start_fuel_miles=0)
    # 100 mi @ $5 to reach the cheap one, then 300 mi @ $3
    assert [round(s.gallons, 6) for s in plan.stops] == [10, 30]
    assert plan.total_cost == pytest.approx(10 * 5 + 30 * 3)


def test_fills_up_when_everything_ahead_is_pricier():
    stations = [Station(0, 3.0), Station(400, 4.0), Station(450, 5.0)]
    plan = plan_fuel_stops(stations, 800, max_range=500, mpg=10, start_fuel_miles=0)
    assert plan.stops[0].gallons == pytest.approx(50)  # full tank at $3
    assert plan.stops[1].station.mile == 400  # then cheapest in range
    assert plan.total_cost == pytest.approx(50 * 3 + 30 * 4)


def test_short_trip_with_full_tank_needs_no_stops():
    plan = plan_fuel_stops([Station(10, 3.0)], 450)
    assert plan.stops == [] and plan.total_cost == 0


def test_gap_longer_than_range_is_reported():
    with pytest.raises(NoFeasiblePlan):
        plan_fuel_stops([Station(100, 3.0), Station(700, 3.0)], 900)


def test_empty_tank_and_no_station_at_origin_is_infeasible():
    with pytest.raises(NoFeasiblePlan, match=r"fuel on board lasts 0 miles.*first station on the route is at mile 10"):
        plan_fuel_stops([Station(10, 3.0)], 100, start_fuel_miles=0)


def test_infeasible_message_names_the_gap():
    with pytest.raises(NoFeasiblePlan, match=r"between mile 100 and the next station \(mile 700\): 600 miles"):
        plan_fuel_stops_dp([Station(100, 3.0), Station(700, 3.0)], 900)


def test_never_exceeds_tank_or_runs_dry():
    rng = random.Random(7)
    stations = [Station(rng.uniform(0, 3000), rng.uniform(3, 5)) for _ in range(400)]
    plan = plan_fuel_stops(stations, 3000)
    fuel, pos = 500.0, 0.0
    for stop in plan.stops:
        fuel -= stop.station.mile - pos
        assert fuel >= -1e-6
        fuel += stop.gallons * 10
        assert fuel <= 500 + 1e-6
        pos = stop.station.mile
    assert fuel - (3000 - pos) == pytest.approx(0, abs=1e-6)  # arrive empty: no wasted purchase


@pytest.mark.parametrize("seed", range(300))
def test_greedy_matches_bruteforce_dp(seed):
    rng = random.Random(seed)
    cap = rng.randint(5, 40)
    route = rng.randint(cap // 2, cap * 6)
    start = rng.randint(0, cap)
    # integer mile markers; sprinkle enough stations that most instances are feasible
    stations = [Station(rng.randint(0, route), round(rng.uniform(2.5, 6.5), 2)) for _ in range(rng.randint(1, 25))]
    expected = dp_min_cost(stations, route, cap, 10, start)
    try:
        got = plan_fuel_stops(stations, route, max_range=cap, mpg=10, start_fuel_miles=start).total_cost
    except NoFeasiblePlan:
        got = float("inf")
    assert got == pytest.approx(expected)


@pytest.mark.parametrize("seed", range(150))
@pytest.mark.parametrize("stop_cost", [0.0, 0.7, 4.0])
def test_dp_matches_bruteforce_with_stop_cost(seed, stop_cost):
    rng = random.Random(seed)
    cap = rng.randint(5, 30)
    route = rng.randint(cap // 2, cap * 5)
    start = rng.randint(0, cap)
    stations = [Station(rng.randint(0, route), round(rng.uniform(2.5, 6.5), 2)) for _ in range(rng.randint(1, 15))]
    expected = dp_min_cost(stations, route, cap, 10, start, stop_cost)
    try:
        plan = plan_fuel_stops_dp(stations, route, max_range=cap, mpg=10, start_fuel_miles=start,
                                  stop_cost=stop_cost, unit_miles=1.0)
        got = plan.total_cost + stop_cost * len(plan.stops)
    except NoFeasiblePlan:
        got = float("inf")
    assert got == pytest.approx(expected)


@pytest.mark.parametrize("seed", range(50))
def test_dp_without_stop_cost_matches_greedy_on_real_scale(seed):
    rng = random.Random(seed)
    stations = [Station(rng.uniform(0, 2800), rng.uniform(3, 5)) for _ in range(rng.randint(20, 300))]
    try:
        greedy = plan_fuel_stops(stations, 2800).total_cost
    except NoFeasiblePlan:
        return
    assert plan_fuel_stops_dp(stations, 2800).total_cost == pytest.approx(greedy, abs=0.05)


def test_stop_cost_trades_a_little_fuel_for_fewer_stops():
    rng = random.Random(3)
    stations = [Station(rng.uniform(0, 2800), rng.uniform(3, 5)) for _ in range(300)]
    pure = plan_fuel_stops_dp(stations, 2800, stop_cost=0)
    lean = plan_fuel_stops_dp(stations, 2800, stop_cost=5)
    assert len(lean.stops) < len(pure.stops)
    assert lean.total_cost >= pure.total_cost - 1e-9
    assert lean.total_cost + 5 * len(lean.stops) <= pure.total_cost + 5 * len(pure.stops)


def test_reserve_is_never_planned_away():
    rng = random.Random(5)
    stations = [Station(rng.uniform(0, 2800), rng.uniform(3, 5)) for _ in range(300)]
    plan = plan_fuel_stops_dp(stations, 2800, reserve_miles=60, stop_cost=2)
    fuel, pos = 500.0, 0.0
    for stop in plan.stops:
        fuel -= stop.station.mile - pos
        assert fuel >= 60 - 0.2 and stop.fuel_on_arrival_gallons * 10 == pytest.approx(fuel, abs=0.2)
        fuel += stop.gallons * 10
        assert fuel <= 500 + 0.1  # the DP rounds mile markers to 0.1 mi
        pos = stop.station.mile
    assert fuel - (2800 - pos) >= 60 - 0.2


# Regression cases from an external review that claimed co-located stations and
# the origin were mishandled. Several stations share a mile marker whenever they
# share a city centroid, so these orderings happen with real data.
@pytest.mark.parametrize("order", [[3.80, 3.50, 3.10], [3.10, 3.50, 3.80], [3.50, 3.80, 3.10]])
def test_colocated_stations_use_cheapest_regardless_of_order(order):
    stations = [Station(0, 4.50)] + [Station(200, p) for p in order]
    plan = plan_fuel_stops(stations, 650, start_fuel_miles=0)
    assert plan.total_cost == pytest.approx(dp_min_cost(stations, 650, 500, 10, 0))
    assert {round(s.station.price, 2) for s in plan.stops} == {4.50, 3.10}


def test_full_tank_start_still_finds_cheapest_in_first_window():
    # Pricey station just after the origin must not hide the cheap one further on.
    stations = [Station(10, 4.50), Station(300, 2.90), Station(600, 3.40)]
    plan = plan_fuel_stops(stations, 1100)
    assert plan.total_cost == pytest.approx(dp_min_cost(stations, 1100, 500, 10, 500))
    assert [s.station.price for s in plan.stops] == [2.90, 3.40]  # $4.50 never used
