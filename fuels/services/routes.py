import math
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

DIRECTIONS_URL = (
    'https://api.heigit.org/openrouteservice/v2/directions/driving-car/geojson'
)
GEOCODING_URL = 'https://api.heigit.org/pelias/v1'
METERS_PER_MILE = 1609.344
MAX_WAYPOINTS = 50


class RoutingError(Exception):
    def __init__(
        self, message, *, status_code=None, provider_code=None, retry_after=None
    ):
        super().__init__(message)
        self.status_code = status_code
        self.provider_code = provider_code
        self.retry_after = retry_after


@dataclass(frozen=True)
class Route:
    coordinates: tuple[tuple[float, float], ...]
    distance_meters: float
    duration_seconds: float
    leg_distances_meters: tuple[float, ...] = ()
    waypoint_coordinates: tuple[tuple[float, float], ...] = ()

    @property
    def distance_miles(self):
        return self.distance_meters / METERS_PER_MILE

    @property
    def geometry(self):
        return {
            'type': 'LineString',
            'coordinates': [list(point) for point in self.coordinates],
        }


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError('Expected a finite number.')
    try:
        value = float(value)
    except (ValueError, OverflowError):
        raise ValueError('Expected a finite number.') from None
    if not math.isfinite(value):
        raise ValueError('Expected a finite number.')
    return value


def _point(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError('Coordinates must be a (longitude, latitude) pair.')
    longitude, latitude = map(_number, value)
    if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
        raise ValueError(
            'Longitude must be within [-180, 180] and latitude within [-90, 90].'
        )
    return longitude, latitude


def _parse_legs(properties, coordinates, point_count):
    segments = properties['segments']
    indices = properties['way_points']
    if len(segments) != point_count - 1 or len(indices) != point_count:
        raise ValueError
    distances = tuple(_number(segment['distance']) for segment in segments)
    if any(distance < 0 for distance in distances):
        raise ValueError
    if any(
        type(index) is not int or not 0 <= index < len(coordinates) for index in indices
    ):
        raise ValueError
    if (
        indices != sorted(indices)
        or indices[0] != 0
        or indices[-1] != len(coordinates) - 1
    ):
        raise ValueError
    total = _number(properties['summary']['distance'])
    # ORS rounds each leg and the summary independently to tenths of a meter.
    if not math.isclose(
        math.fsum(distances), total, rel_tol=0, abs_tol=0.1 * point_count
    ):
        raise ValueError
    return distances, tuple(coordinates[index] for index in indices)


def _parse_route(data, *, point_count=None):
    try:
        if (
            data['type'] != 'FeatureCollection'
            or not isinstance(data['features'], list)
            or len(data['features']) != 1
        ):
            raise ValueError
        feature = data['features'][0]
        if feature['type'] != 'Feature' or feature['geometry']['type'] != 'LineString':
            raise ValueError
        points = feature['geometry']['coordinates']
        if not isinstance(points, list) or len(points) < 2:
            raise ValueError
        coordinates = tuple(_point(point) for point in points)
        summary = feature['properties']['summary']
        distance = _number(summary['distance'])
        duration = _number(summary['duration'])
        if distance < 0 or duration < 0:
            raise ValueError
        legs, waypoints = (), ()
        if point_count is not None:
            legs, waypoints = _parse_legs(
                feature['properties'], coordinates, point_count
            )
            distance = math.fsum(legs)
        return Route(coordinates, distance, duration, legs, waypoints)
    except (KeyError, TypeError, ValueError):
        raise RoutingError('ORS returned an invalid route response.') from None


def _raise_response_error(response, service):
    code = None
    with suppress(ValueError, AttributeError):
        code = response.json().get('error', {}).get('code')
    if response.status_code in (401, 403):
        message = 'ORS rejected the API key or its permissions.'
    elif response.status_code == 429:
        message = f'ORS {service} rate limit reached. Try again later.'
    elif service == 'routing' and code in (2009, 2010):
        message = 'ORS could not find a drivable route between these coordinates.'
    elif response.status_code >= 500:
        message = f'ORS {service} service is temporarily unavailable.'
    else:
        message = f'ORS rejected the {service} request (HTTP {response.status_code}).'
    raise RoutingError(
        message,
        status_code=response.status_code,
        provider_code=code if service == 'routing' else None,
        retry_after=response.headers.get('Retry-After'),
    )


class ORSRoutingClient:
    """ORS directions and geocoding with one lazily opened HTTP connection pool.

    Own one instance per request or command and close it with a context manager.
    Fully cached requests need neither an HTTP client nor an API key.
    """

    def __init__(self, api_key=None, *, transport=None):
        self._api_key = api_key
        self._transport = transport
        self._client = None
        self._closed = False

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback):
        self.close()

    def close(self):
        if self._client is not None:
            self._client.close()
        self._closed = True

    def _http_client(self):
        if self._closed:
            raise RuntimeError('ORS client is closed.')
        if self._client is None:
            api_key = self._api_key
            if api_key is None:
                api_key = getattr(settings, 'ORS_API_KEY', '')
            if not isinstance(api_key, str) or not api_key.strip():
                raise ImproperlyConfigured(
                    'Set ORS_API_KEY before making ORS requests.'
                )
            self._client = httpx.Client(
                headers={'Authorization': api_key.strip()},
                timeout=httpx.Timeout(30.0, connect=5.0),
                follow_redirects=False,
                transport=self._transport,
            )
        return self._client

    def _request(self, method, url, *, service, **kwargs):
        try:
            response = self._http_client().request(method, url, **kwargs)
        except httpx.TimeoutException:
            raise RoutingError(f'ORS {service} request timed out.') from None
        except httpx.RequestError:
            raise RoutingError(
                f'Could not connect to the ORS {service} service.'
            ) from None
        if response.status_code != 200:
            _raise_response_error(response, service)
        try:
            return response.json()
        except ValueError:
            raise RoutingError('ORS returned invalid JSON.') from None

    def _geocode(self, path, params):
        data = self._request(
            'GET',
            f'{GEOCODING_URL}/{path}',
            service='location',
            params=params,
            headers={'Accept': 'application/json'},
            timeout=httpx.Timeout(15.0, connect=5.0),
        )
        try:
            if data['type'] != 'FeatureCollection' or not isinstance(
                data['features'], list
            ):
                raise ValueError
            return data['features']
        except (KeyError, TypeError, ValueError):
            raise RoutingError('ORS returned an invalid location response.') from None

    def search_location(
        self, text, *, country='USA', layers=None, sources=None, size=10
    ):
        """Return raw candidates; callers apply their location matching rules."""
        params = {'text': text, 'boundary.country': country, 'size': size}
        if layers is not None:
            params['layers'] = layers
        if sources is not None:
            params['sources'] = sources
        return self._geocode('search', params)

    def reverse_location(self, longitude, latitude, *, layers='country', size=1):
        """Look up a coordinate without applying a country search bias."""
        longitude, latitude = _point((longitude, latitude))
        return self._geocode(
            'reverse',
            {
                'point.lon': longitude,
                'point.lat': latitude,
                'layers': layers,
                'size': size,
            },
        )

    def get_route(self, start, end, *, waypoints=()):
        """Return a driving route through start, ordered waypoints, and end.

        This validates coordinate ranges, not whether the endpoints are in the
        USA. Country validation is handled during endpoint resolution.
        No automatic retries: each lookup uses at most one routing request.
        """
        start, end = _point(start), _point(end)
        points = (start, *(_point(point) for point in waypoints), end)
        if len(points) > MAX_WAYPOINTS:
            raise ValueError(f'ORS supports at most {MAX_WAYPOINTS} waypoints.')
        via_stations = len(points) > 2
        data = self._request(
            'POST',
            DIRECTIONS_URL,
            service='routing',
            headers={'Accept': 'application/geo+json'},
            json={
                'coordinates': [list(point) for point in points],
                'units': 'm',
                'instructions': via_stations,
                'elevation': False,
            },
        )
        return _parse_route(data, point_count=len(points) if via_stations else None)
