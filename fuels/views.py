from dataclasses import asdict
from functools import wraps

from django.core.exceptions import ImproperlyConfigured, RequestDataTooBig
from django.http import HttpResponse, JsonResponse
from ninja import Router
from ninja.decorators import decorate_view

from .models import FuelStation
from .schemas import ErrorResponse, RoutePlanRequest, RoutePlanResponse
from .services.cache import get_cached_route
from .services.fuel_planner import InsufficientFuelCoverage
from .services.itinerary import ItineraryError, plan_itinerary
from .services.locations import US_STATE_CODES, LocationError, resolve_location
from .services.proximity import calculate_proximity_miles
from .services.routes import METERS_PER_MILE, ORSRoutingClient, RoutingError
from .services.stations import find_stations_along_route

router = Router(tags=['Routes'])


def _error(code, message, status, **details):
    body = ErrorResponse(error={'code': code, 'message': message, **details})
    return JsonResponse(body.model_dump(exclude_none=True), status=status)


def require_json_body(view):
    """Apply transport limits before Ninja reads and validates the payload."""

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if request.content_type != 'application/json':
            return _error(
                'unsupported_media_type', 'Content-Type must be application/json.', 415
            )
        try:
            if len(request.body) > 4096:
                return _error(
                    'request_too_large', 'Request body must not exceed 4096 bytes.', 413
                )
        except RequestDataTooBig:
            return _error('request_too_large', 'Request body is too large.', 413)
        return view(request, *args, **kwargs)

    return wrapped


@router.post(
    '/plan/',
    response={
        200: RoutePlanResponse,
        400: ErrorResponse,
        413: ErrorResponse,
        415: ErrorResponse,
        422: ErrorResponse,
        502: ErrorResponse,
        503: ErrorResponse,
    },
    url_name='plan-route',
    summary='Plan a route and fuel stops',
)
@decorate_view(require_json_body)
def plan_route(request, payload: RoutePlanRequest, response: HttpResponse):
    """Plan a driving itinerary using cached directions and current station prices."""
    ratio, cap = payload.proximity_ratio, payload.max_proximity_miles

    field = 'start'
    try:
        with ORSRoutingClient() as client:
            start, start_hit = resolve_location(payload.start, client=client)
            field = 'end'
            end, end_hit = resolve_location(payload.end, client=client)
            route, route_hit = get_cached_route(
                start.coordinates, end.coordinates, client=client
            )
            candidates = find_stations_along_route(
                route,
                ratio=ratio,
                max_miles=cap,
                stations=FuelStation.objects.filter(state__in=US_STATE_CODES),
            )
            proximity = calculate_proximity_miles(
                route.distance_miles, ratio=ratio, max_miles=cap
            )
            itinerary = plan_itinerary(
                start.coordinates,
                end.coordinates,
                route,
                candidates,
                starting_fuel_gallons=payload.starting_fuel_gallons,
                base_cache_hit=route_hit,
                client=client,
            )
            route, plan = itinerary.route, itinerary.plan
            route_hit = route_hit and itinerary.cache_hit
    except LocationError as error:
        return _error(
            error.code, str(error), 422, field=field, candidates=error.candidates
        )
    except ItineraryError as error:
        return _error('unverified_fuel_itinerary', str(error), 422)
    except InsufficientFuelCoverage as error:
        return _error(
            'insufficient_fuel_coverage',
            str(error),
            422,
            from_mile=error.from_mile,
            to_mile=error.to_mile,
            available_range_miles=error.available_range_miles,
        )
    except RoutingError as error:
        no_route = error.provider_code in (2009, 2010)
        status = 422 if no_route else 503 if error.status_code == 429 else 502
        code = (
            'route_not_found'
            if no_route
            else 'upstream_rate_limited'
            if error.status_code == 429
            else 'upstream_error'
        )
        response = _error(code, str(error), status)
        if (
            error.retry_after
            and '\n' not in error.retry_after
            and '\r' not in error.retry_after
        ):
            response['Retry-After'] = error.retry_after
        return response
    except ImproperlyConfigured:
        return _error(
            'service_not_configured',
            'The location/routing service is not configured.',
            503,
        )

    stops = []
    for stop in plan.stops:
        station = stop.station
        stops.append(
            {
                'opis_id': station.opis_truckstop_id,
                'name': station.truckstop_name,
                'address': station.address,
                'city': station.city,
                'state': station.state,
                'coordinates': [float(station.longitude), float(station.latitude)],
                'distance_along_route_miles': stop.distance_along_route_miles,
                'gallons_to_buy': stop.gallons_to_buy,
                'price_per_gallon': stop.price_per_gallon,
                'purchase_cost': stop.purchase_cost,
                'fuel_on_arrival_gallons': stop.fuel_on_arrival_gallons,
                'fuel_on_departure_gallons': stop.fuel_on_departure_gallons,
            }
        )
    response['Cache-Control'] = (
        'no-store'  # Fuel prices are read afresh on every request.
    )
    return {
        'start': asdict(start),
        'end': asdict(end),
        'route': {
            'geometry': route.geometry,
            'distance_miles': route.distance_miles,
            'distance_meters': route.distance_meters,
            'duration_seconds': route.duration_seconds,
            'waypoints': itinerary.waypoints,
            'snapped_waypoints': route.waypoint_coordinates
            or (route.coordinates[0], route.coordinates[-1]),
            'leg_distances_miles': [
                distance / METERS_PER_MILE
                for distance in (route.leg_distances_meters or (route.distance_meters,))
            ],
        },
        'proximity_miles': proximity,
        'candidate_count': len(candidates),
        'stops': stops,
        'fuel': {
            'starting_fuel_gallons': plan.starting_fuel_gallons,
            'fuel_used_gallons': plan.fuel_used_gallons,
            'fuel_remaining_gallons': plan.fuel_remaining_gallons,
            'total_gallons_purchased': plan.total_gallons_purchased,
            'total_purchase_cost': plan.total_purchase_cost,
            'currency': 'USD',
        },
        'cache': {'start': start_hit, 'end': end_hit, 'route': route_hit},
        'assumptions': {
            'driving_detours_included': True,
            'optimization_scope': 'selected_itinerary',
            'starting_fuel_cost_included': False,
            'station_coverage': 'geocoded_stations_only',
        },
    }
