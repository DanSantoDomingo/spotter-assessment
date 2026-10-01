"""Choose how far from a route to search for candidate fuel stations."""

import math
from decimal import Decimal


def calculate_proximity_miles(
    route_distance_miles: float,
    *,
    ratio: float = 0.10,
    max_miles: float = 5.0,
) -> float:
    """Scale the search radius with route length, up to a configurable cap.

    Pass Route.distance_miles, not its distance in meters. The result is the
    maximum geographic distance from the route, not added driving distance.
    A zero-length route returns zero; there is no minimum radius that could
    exceed a short route's length. A ratio between 0 and 1 keeps the radius
    at or below the total route distance.
    """
    values = []
    for name, value in (
        ('route_distance_miles', route_distance_miles),
        ('ratio', ratio),
        ('max_miles', max_miles),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise ValueError(f'{name} must be a finite, nonnegative number.')
        try:
            value = float(value)
        except (ValueError, OverflowError):
            raise ValueError(f'{name} must be a finite, nonnegative number.') from None
        if not math.isfinite(value) or value < 0:
            raise ValueError(f'{name} must be a finite, nonnegative number.')
        values.append(value)

    distance, ratio, cap = values
    if ratio > 1:
        raise ValueError('ratio must be between 0 and 1.')
    return min(distance * ratio, cap)
