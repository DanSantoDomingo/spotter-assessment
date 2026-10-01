"""Find geocoded fuel stations near a route without external API calls."""

import math
from dataclasses import dataclass
from itertools import pairwise

from fuels.models import FuelStation

from .proximity import calculate_proximity_miles
from .routes import METERS_PER_MILE, Route, _point

EARTH_RADIUS_METERS = 6_371_008.8


@dataclass(frozen=True)
class NearbyStation:
    station: FuelStation
    distance_from_route_miles: float
    distance_along_route_miles: float


def _angular_distance(start, end):
    """Great-circle distance in radians; input points are (lon, lat) radians."""
    lon1, lat1 = start
    lon2, lat2 = end
    haversine = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * math.asin(math.sqrt(min(1, max(0, haversine))))


def _bearing(start, end):
    lon1, lat1 = start
    lon2, lat2 = end
    return math.atan2(
        math.sin(lon2 - lon1) * math.cos(lat2),
        math.cos(lat1) * math.sin(lat2)
        - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1),
    )


def _nearest_on_segment(point, start, end, length, bearing):
    """Return (distance from segment, progress on segment), both in radians.
    https://www.movable-type.co.uk/scripts/latlong.html
    """
    distance = _angular_distance(start, point)
    angle = _bearing(start, point) - bearing
    along = math.atan2(math.sin(distance) * math.cos(angle), math.cos(distance))
    if along <= 0:
        return distance, 0.0
    if along >= length:
        return _angular_distance(end, point), length
    cross = math.asin(min(1, max(-1, math.sin(distance) * math.sin(angle))))
    return abs(cross), along


def _candidates(stations, points, padding):
    longitudes, latitudes = zip(*points, strict=True)
    south = max(-math.pi / 2, min(latitudes) - padding)
    north = min(math.pi / 2, max(latitudes) + padding)
    candidates = stations.filter(
        latitude__isnull=False,
        longitude__isnull=False,
        latitude__gte=math.degrees(south),
        latitude__lte=math.degrees(north),
    )
    extreme_latitude = max(abs(south), abs(north))
    # Near poles or across the date line, retain all longitudes to avoid losing
    # candidates. This also supports routes in Alaska.
    if extreme_latitude < math.pi / 2 and max(longitudes) - min(longitudes) < math.pi:
        longitude_padding = padding / math.cos(extreme_latitude)
        west, east = (
            min(longitudes) - longitude_padding,
            max(longitudes) + longitude_padding,
        )
        if west >= -math.pi and east <= math.pi:
            candidates = candidates.filter(
                longitude__gte=math.degrees(west), longitude__lte=math.degrees(east)
            )
    return candidates


def _route_segments(points):
    segments = []
    total_angle = 0.0
    for start, end in pairwise(points):
        length = _angular_distance(start, end)
        if length >= math.pi - 1e-10:
            raise ValueError('Route contains an ambiguous antipodal segment.')
        if length > 0:
            segments.append((start, end, length, _bearing(start, end), total_angle))
            total_angle += length
    return segments, total_angle


def find_stations_along_route(
    route: Route,
    *,
    ratio: float = 0.10,
    max_miles: float = 5.0,
    stations=None,
) -> list[NearbyStation]:
    """Return candidates ordered by their nearest position along the route.

    Radius is calculated from the route's total distance. Optionally pass a
    FuelStation queryset to restrict the search, for example to Texas.

    Distances use a spherical Earth approximation. Progress along the geometry
    is scaled to ORS's total route distance. These are candidate estimates, not
    road-access or driving-detour measurements. Each station appears once; when
    equally near multiple segments, its earliest position is used.
    """
    proximity = calculate_proximity_miles(
        route.distance_miles, ratio=ratio, max_miles=max_miles
    )
    if not isinstance(route.coordinates, (list, tuple)) or len(route.coordinates) < 2:
        raise ValueError('Route geometry must contain at least two coordinate pairs.')
    points = [tuple(map(math.radians, _point(point))) for point in route.coordinates]
    segments, total_angle = _route_segments(points)
    if route.distance_miles == 0:
        return []
    if not segments:
        raise ValueError('A positive-distance route must contain distinct coordinates.')

    radius = proximity * METERS_PER_MILE / EARTH_RADIUS_METERS
    # Every point on a segment lies within half its length of an endpoint.
    # This padding keeps SQL bounds safe even for sparse, curved geometry.
    padding = radius + max(segment[2] for segment in segments) / 2
    queryset = FuelStation.objects.all() if stations is None else stations
    candidates = _candidates(queryset, points, padding)
    matches = []
    for station in candidates.iterator():
        try:
            point = tuple(
                map(math.radians, _point((station.longitude, station.latitude)))
            )
        except ValueError:
            continue  # Invalid coordinates must not influence route planning.
        closest, progress = math.inf, 0.0
        for start, end, length, bearing, offset in segments:
            distance, along = _nearest_on_segment(point, start, end, length, bearing)
            if distance < closest - 1e-12:
                closest, progress = distance, offset + along
        if closest <= radius + 1e-12:
            matches.append(
                NearbyStation(
                    station=station,
                    distance_from_route_miles=closest
                    * EARTH_RADIUS_METERS
                    / METERS_PER_MILE,
                    distance_along_route_miles=progress
                    / total_angle
                    * route.distance_miles,
                )
            )
    return sorted(
        matches,
        key=lambda match: (
            match.distance_along_route_miles,
            match.station.opis_truckstop_id,
        ),
    )
