"""Validate fuel purchases against the driving route through selected stations."""

from dataclasses import dataclass
from decimal import Decimal

from .cache import get_cached_route
from .fuel_planner import FuelPlan, InsufficientFuelCoverage, plan_fuel_stops
from .routes import MAX_WAYPOINTS, METERS_PER_MILE, Route
from .stations import NearbyStation


class ItineraryError(Exception):
    """The selected itinerary cannot be validated; other stations may work."""


@dataclass(frozen=True)
class Itinerary:
    route: Route
    plan: FuelPlan
    cache_hit: bool
    waypoints: tuple[tuple[float, float], ...]


def plan_itinerary(
    start, end, base_route, candidates, *, starting_fuel_gallons, base_cache_hit=False
):
    preliminary = plan_fuel_stops(
        base_route,
        candidates,
        starting_fuel_gallons=starting_fuel_gallons,
    )
    selected = tuple(stop.station for stop in preliminary.stops)
    if not selected:
        return Itinerary(base_route, preliminary, base_cache_hit, (start, end))
    if len(selected) + 2 > MAX_WAYPOINTS:
        raise ItineraryError(
            'The selected itinerary exceeds the routing waypoint limit.'
        )

    waypoints = tuple(
        (float(station.longitude), float(station.latitude)) for station in selected
    )
    route, cache_hit = get_cached_route(start, end, waypoints=waypoints)
    # Use returned leg distances, never the old nearest-point projections.
    progress = Decimal('0')
    actual_candidates = []
    for station, meters in zip(selected, route.leg_distances_meters[:-1], strict=True):
        progress += Decimal(str(meters)) / Decimal(str(METERS_PER_MILE))
        actual_candidates.append(NearbyStation(station, 0, progress))
    try:
        plan = plan_fuel_stops(
            route,
            actual_candidates,
            starting_fuel_gallons=starting_fuel_gallons,
        )
    except InsufficientFuelCoverage as error:
        raise ItineraryError(
            f'The selected stations are not reachable on the driving route: {error} '
            'No feasible plan has been verified; other stations may be needed.'
        ) from error
    # Retain all routed waypoints, even if recalculation skips a purchase there.
    # Removing a waypoint would invalidate the geometry and distances just checked.
    return Itinerary(route, plan, cache_hit, (start, *waypoints, end))
