"""Cache external data; station prices and fuel plans are computed afresh."""

import hashlib
import json

from django.conf import settings
from django.core.cache import caches

from .routes import DIRECTIONS_URL, ORSRoutingClient, _point


def cache_key(kind, value):
    serialized = json.dumps(
        value, sort_keys=True, separators=(',', ':'), ensure_ascii=False
    )
    digest = hashlib.sha256(serialized.encode('utf-8')).hexdigest()
    return f'fuel-routing:v1:{kind}:{digest}'


def get_cached_route(start, end, *, waypoints=()):
    """Return (route, cache_hit). Direction and coordinate precision are preserved."""
    start, end = _point(start), _point(end)
    waypoints = tuple(_point(point) for point in waypoints)
    cache = caches['routing']
    key = cache_key('route-v2', [DIRECTIONS_URL, start, *waypoints, end])
    route = cache.get(key)
    if route is not None:
        return route, True
    with ORSRoutingClient() as client:
        route = client.get_route(start, end, waypoints=waypoints)
    cache.set(key, route, timeout=settings.ROUTE_CACHE_SECONDS)
    return route, False
