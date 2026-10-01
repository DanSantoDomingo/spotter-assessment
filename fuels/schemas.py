"""Public request and response contracts for route planning."""

from decimal import Decimal
from typing import Annotated, Literal

from ninja import Schema
from pydantic import ConfigDict, Field, StrictFloat, field_validator

from .services.locations import validate_location

Longitude = Annotated[StrictFloat, Field(ge=-180, le=180, allow_inf_nan=False)]
Latitude = Annotated[StrictFloat, Field(ge=-90, le=90, allow_inf_nan=False)]
Coordinates = tuple[Longitude, Latitude]
PlaceName = Annotated[str, Field(min_length=2, max_length=300)]


class RoutePlanRequest(Schema):
    model_config = ConfigDict(
        extra='forbid',
        json_schema_extra={
            'examples': [
                {
                    'start': 'El Paso, TX',
                    'end': 'Houston, TX',
                    'starting_fuel_gallons': 50,
                    'proximity_ratio': 0.1,
                    'max_proximity_miles': 5,
                }
            ]
        },
    )

    start: PlaceName | Coordinates = Field(
        description='US place/address or [longitude, latitude].'
    )
    end: PlaceName | Coordinates = Field(
        description='US place/address or [longitude, latitude].'
    )
    starting_fuel_gallons: StrictFloat = Field(ge=0, le=50, allow_inf_nan=False)
    proximity_ratio: StrictFloat = Field(default=0.10, ge=0, le=1, allow_inf_nan=False)
    max_proximity_miles: StrictFloat = Field(
        default=5.0,
        ge=0,
        allow_inf_nan=False,
        description='Cap on geographic distance from the route in miles.',
    )

    @field_validator('start', 'end', mode='before')
    @classmethod
    def normalize_location(_cls, value):
        return validate_location(value)


class LocationResponse(Schema):
    label: str
    coordinates: Coordinates


class RouteGeometry(Schema):
    type: Literal['LineString']
    coordinates: list[Coordinates]


class RouteResponse(Schema):
    geometry: RouteGeometry
    distance_miles: float
    distance_meters: float
    duration_seconds: float
    waypoints: list[Coordinates] = Field(
        description='Ordered requested stops, including start/end. '
        'Retained even if no fuel is bought at a waypoint.'
    )
    snapped_waypoints: list[Coordinates] = Field(
        description='Road positions returned by ORS for the routed fuel itinerary.'
    )
    leg_distances_miles: list[float]


class FuelStopResponse(Schema):
    opis_id: int
    name: str
    address: str
    city: str
    state: str
    coordinates: Coordinates
    distance_along_route_miles: Decimal
    gallons_to_buy: Decimal
    price_per_gallon: Decimal
    purchase_cost: Decimal
    fuel_on_arrival_gallons: Decimal
    fuel_on_departure_gallons: Decimal


class FuelResponse(Schema):
    starting_fuel_gallons: Decimal
    fuel_used_gallons: Decimal
    fuel_remaining_gallons: Decimal
    total_gallons_purchased: Decimal
    total_purchase_cost: Decimal
    currency: Literal['USD']


class CacheResponse(Schema):
    start: bool
    end: bool
    route: bool


class PlanningAssumptions(Schema):
    driving_detours_included: Literal[True]
    optimization_scope: Literal['selected_itinerary']
    starting_fuel_cost_included: Literal[False]
    station_coverage: Literal['geocoded_stations_only']


class RoutePlanResponse(Schema):
    start: LocationResponse
    end: LocationResponse
    route: RouteResponse
    proximity_miles: float
    candidate_count: int
    stops: list[FuelStopResponse]
    fuel: FuelResponse
    cache: CacheResponse
    assumptions: PlanningAssumptions


class ErrorDetail(Schema):
    code: str
    message: str
    field: str | None = None
    candidates: list[LocationResponse] | None = None
    from_mile: Decimal | None = None
    to_mile: Decimal | None = None
    available_range_miles: Decimal | None = None


class ErrorResponse(Schema):
    error: ErrorDetail
