"""Cost-optimal refuelling along a fixed route.

Stations sit at known mile markers with known prices, the tank holds
`max_range` miles of fuel, and we choose how much to buy at each station to
reach the destination. Two solvers:

* `plan_fuel_stops_dp` - what the API runs. It minimises pump cost plus a
  fixed `stop_cost` per stop (the time and detour a stop costs a trucker) and
  keeps a `reserve_miles` buffer in the tank. Pure pump cost, the objective of
  the greedy below, happily makes many tiny top-ups and arrives everywhere on
  fumes; a few dollars per stop removes about half the stops on a
  coast-to-coast trip for a few dollars more fuel.
* `plan_fuel_stops` - the exact greedy for pure pump cost, kept as the
  reference the tests check the DP against (stop_cost=0, no reserve). Because
  cost is linear in fuel, this rule is provably optimal (Khuller, Malekian &
  Mestre, "To fill or not to fill", 2007):

    At the current station, look one full tank ahead.
    * If a cheaper station is in range, buy just enough to reach the *nearest*
      cheaper one and go there.
    * Else, if the destination is in range, buy just enough to finish.
    * Else, fill the tank and go to the *cheapest* station in range.
"""

from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np

EPS = 1e-9


class NoFeasiblePlan(Exception):
    """Some stretch of the route cannot be driven on the fuel available."""

    def __init__(self, message: str, at_mile: float, next_mile: float):
        super().__init__(message)
        self.at_mile = at_mile
        self.next_mile = next_mile


@dataclass(frozen=True)
class Station:
    mile: float  # distance along the route from the start
    price: float  # $ per gallon
    ref: Any = None  # caller's payload (model instance, dict, ...)


@dataclass(frozen=True)
class FuelStop:
    station: Station
    gallons: float
    cost: float
    fuel_on_arrival_gallons: float


@dataclass
class FuelPlan:
    stops: list[FuelStop] = field(default_factory=list)
    route_miles: float = 0.0
    mpg: float = 10.0
    start_fuel_gallons: float = 0.0

    @property
    def total_cost(self) -> float:
        return sum(s.cost for s in self.stops)

    @property
    def total_gallons(self) -> float:
        return sum(s.gallons for s in self.stops)


def _on_route(stations: Sequence[Station], route_miles: float) -> list[Station]:
    return sorted((s for s in stations if -EPS <= s.mile <= route_miles + EPS), key=lambda s: s.mile)


def check_reachable(pts: Sequence[Station], route_miles: float, start_miles: float, tank_miles: float) -> None:
    """Raise NoFeasiblePlan naming the gap that cannot be crossed, if there is one.

    `pts` are sorted by mile. Refuelling to a full tank is allowed at every
    station, so the trip is feasible iff each next station (and finally the
    destination) lies within the farthest point reachable so far.
    """
    farthest = start_miles  # absolute mile marker
    last = None  # last station reached
    for s in pts:
        if farthest >= route_miles - EPS or s.mile > farthest + EPS:
            break
        farthest = max(farthest, s.mile + tank_miles)
        last = s
    if farthest >= route_miles - EPS:
        return
    nxt = next((s.mile for s in pts if s.mile > farthest + EPS), route_miles)
    if last is None:
        first = f"the first station on the route is at mile {nxt:.0f}" if nxt < route_miles else "no station is in reach"
        raise NoFeasiblePlan(
            f"The fuel on board lasts {start_miles:.0f} miles, but {first}. "
            "Start with more fuel or widen corridor_miles.",
            0.0,
            nxt,
        )
    what = "the destination" if nxt >= route_miles else f"the next station (mile {nxt:.0f})"
    raise NoFeasiblePlan(
        f"No station between mile {last.mile:.0f} and {what}: {nxt - last.mile:.0f} miles, "
        f"more than one tank ({tank_miles:.0f} usable miles). Try a wider corridor_miles.",
        last.mile,
        nxt,
    )


def plan_fuel_stops_dp(
    stations: Sequence[Station],
    route_miles: float,
    *,
    max_range: float = 500.0,
    mpg: float = 10.0,
    start_fuel_miles: float | None = None,
    stop_cost: float = 0.0,
    reserve_miles: float = 0.0,
    unit_miles: float = 0.1,
) -> FuelPlan:
    """Minimise pump cost + stop_cost per stop, never planning below `reserve_miles` of fuel.

    Dynamic programme over (station, fuel level), with fuel counted in
    `unit_miles` steps. cost[k] is the cheapest way to leave the current
    station holding k units. Buying at c $/unit from level j up to level k
    costs (k - j) * c + stop_cost, so

        new[k] = min(arrived[k], min_{j <= k}(arrived[j] - j*c) + k*c + stop_cost)

    and the inner min is a running prefix minimum: one numpy pass per
    station, O(n * tank/unit) overall (~300 x 5,000 for a coast-to-coast
    trip, a few ms).
    """
    if start_fuel_miles is None:
        start_fuel_miles = max_range
    if not 0 <= reserve_miles < max_range:
        raise ValueError("reserve must be at least 0 and less than a full tank")
    if not reserve_miles - EPS <= start_fuel_miles <= max_range + EPS:
        raise ValueError("start fuel must be between the reserve and a full tank")

    pts = _on_route(stations, route_miles)
    usable = max_range - reserve_miles  # the reserve is never planned to be burned
    check_reachable(pts, route_miles, start_fuel_miles - reserve_miles, usable)

    cap = int(usable / unit_miles + EPS)
    levels = np.arange(cap + 1)
    start_level = min(cap, int(round((start_fuel_miles - reserve_miles) / unit_miles)))
    cost = np.full(cap + 1, np.inf)
    cost[start_level] = 0.0

    history = []  # per station: (station, units driven to reach it, level bought from or -1)
    pos = 0
    for st in pts:
        u = int(round(st.mile / unit_miles))
        d, pos = u - pos, u
        arrived = np.full(cap + 1, np.inf)
        if d <= cap:
            arrived[: cap + 1 - d] = cost[d:]
        c = st.price * unit_miles / mpg
        vals = arrived - levels * c
        best = np.minimum.accumulate(vals)
        argbest = np.maximum.accumulate(np.where(vals == best, levels, 0))
        bought = best + levels * c + stop_cost
        take = bought < arrived - 1e-9  # on a tie, do not stop
        cost = np.where(take, bought, arrived)
        history.append((st, d, np.where(take, argbest, -1)))

    left = int(round(route_miles / unit_miles)) - pos
    if left > cap or not np.isfinite(cost[left:]).any():  # only on rounding at an exact one-tank gap
        raise NoFeasiblePlan("No plan reaches the destination on the fuel and stations available.", pos * unit_miles, route_miles)
    level = left + int(np.argmin(cost[left:]))

    # Walk back from the destination to recover what was bought where.
    buys = []
    for st, d, src in reversed(history):
        j = int(src[level])
        if j >= 0:
            buys.append((st, j, level))
            level = j
        level += d
    assert level == start_level, "DP backtrack lost the starting fuel level"

    plan = FuelPlan(route_miles=route_miles, mpg=mpg, start_fuel_gallons=start_fuel_miles / mpg)
    for st, j, k in reversed(buys):
        gallons = (k - j) * unit_miles / mpg
        plan.stops.append(FuelStop(st, gallons, gallons * st.price, (j * unit_miles + reserve_miles) / mpg))
    return plan


def plan_fuel_stops(
    stations: Sequence[Station],
    route_miles: float,
    *,
    max_range: float = 500.0,
    mpg: float = 10.0,
    start_fuel_miles: float | None = None,
) -> FuelPlan:
    """Exact minimum pump cost (greedy, see module docstring) to drive `route_miles`.

    `start_fuel_miles` is how far the fuel already in the tank will go
    (defaults to a full tank). The vehicle cannot buy fuel at the origin
    unless a station sits at mile 0.
    """
    if start_fuel_miles is None:
        start_fuel_miles = max_range
    if not 0 <= start_fuel_miles <= max_range + EPS:
        raise ValueError("start fuel must be between 0 and a full tank")

    pts = _on_route(stations, route_miles)
    check_reachable(pts, route_miles, start_fuel_miles, max_range)
    plan = FuelPlan(route_miles=route_miles, mpg=mpg, start_fuel_gallons=start_fuel_miles / mpg)

    n = len(pts)
    cur = -1  # index into pts; -1 = the origin, where nothing can be bought
    pos = 0.0
    fuel = start_fuel_miles  # in miles of driving
    price = float("inf")

    def buy(miles: float) -> None:
        miles = min(miles, max_range - fuel)  # `reach + EPS` comparisons can overshoot by EPS
        if miles > EPS:
            gallons = miles / mpg
            plan.stops.append(FuelStop(pts[cur], gallons, gallons * price, fuel / mpg))

    while True:
        if route_miles - pos <= fuel + EPS:
            return plan  # enough fuel on board to finish

        reach = pos + (max_range if cur >= 0 else fuel)
        lo = hi = cur + 1
        while hi < n and pts[hi].mile <= reach + EPS:
            hi += 1
        window = range(lo, hi)  # stations ahead within one tank

        cheaper = next((k for k in window if pts[k].price < price - EPS), None)
        if cheaper is not None:
            nxt = cheaper
            buy((pts[nxt].mile - pos) - fuel)
            fuel = max(fuel, pts[nxt].mile - pos)
        elif route_miles <= reach + EPS:
            buy((route_miles - pos) - fuel)
            return plan
        elif window:
            buy(max_range - fuel)
            fuel = max_range
            # cheapest in range; on ties prefer the farthest (fewer stops)
            nxt = min(window, key=lambda k: (pts[k].price, -pts[k].mile))
        else:  # unreachable after check_reachable(); kept as a guard
            raise NoFeasiblePlan(f"No fuel station within {max_range:.0f} miles after mile {pos:.0f}.", pos, reach)

        fuel = max(0.0, fuel - (pts[nxt].mile - pos))  # never report -0.0 gallons on arrival
        cur, pos, price = nxt, pts[nxt].mile, pts[nxt].price
