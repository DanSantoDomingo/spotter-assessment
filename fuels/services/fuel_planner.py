from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from fuels.models import FuelStation

from .routes import Route
from .stations import NearbyStation

MILES_PER_GALLON = Decimal('10')
MAX_RANGE_MILES = Decimal('500')
TANK_CAPACITY_GALLONS = MAX_RANGE_MILES / MILES_PER_GALLON
# Remove floating-point noise from projected distances: one millionth of a mile.
DISTANCE_PRECISION = Decimal('0.000001')


class InsufficientFuelCoverage(Exception):
    """The available stations cannot bridge a gap with the available fuel."""

    def __init__(self, from_mile, to_mile, available_range_miles):
        super().__init__(
            f'Cannot cover the gap from mile {from_mile:g} to {to_mile:g}; '
            f'available range is {available_range_miles:g} miles.'
        )
        self.from_mile = from_mile
        self.to_mile = to_mile
        self.available_range_miles = available_range_miles


@dataclass(frozen=True)
class FuelStop:
    station: FuelStation
    distance_along_route_miles: Decimal
    fuel_on_arrival_gallons: Decimal
    gallons_to_buy: Decimal
    fuel_on_departure_gallons: Decimal
    price_per_gallon: Decimal
    purchase_cost: Decimal


@dataclass(frozen=True)
class FuelPlan:
    stops: tuple[FuelStop, ...]
    starting_fuel_gallons: Decimal
    fuel_used_gallons: Decimal
    fuel_remaining_gallons: Decimal
    total_gallons_purchased: Decimal
    total_purchase_cost: Decimal


def _decimal(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f'{name} must be a finite number.')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f'{name} must be a finite number.') from None
    if not result.is_finite():
        raise ValueError(f'{name} must be a finite number.')
    return result


def _distance(value, name):
    distance = _decimal(value, name)
    if distance < 0:
        raise ValueError(f'{name} must be nonnegative.')
    try:
        return distance.quantize(DISTANCE_PRECISION)
    except InvalidOperation:
        raise ValueError(f'{name} is too large.') from None


def _prepare_stations(candidates, distance):
    stations = []
    for candidate in candidates:
        position = _distance(candidate.distance_along_route_miles, 'station position')
        if position > distance:
            raise ValueError('Station position cannot be beyond the destination.')
        price = _decimal(candidate.station.retail_price, 'station price')
        if price <= 0:
            raise ValueError('Station price must be positive.')
        if position < distance:
            stations.append((position, price, candidate.station))
    # Prefer the cheapest option when multiple stations share a position.
    stations.sort(key=lambda item: (item[0], item[1], item[2].opis_truckstop_id))

    return stations


def plan_fuel_stops(
    route: Route,
    candidates: list[NearbyStation],
    *,
    starting_fuel_gallons,
) -> FuelPlan:
    """Choose purchases that minimize fuel expense under the fixed-route model.

    Starting fuel is required explicitly (0 to 50 gallons); its acquisition cost
    is excluded. A short trip fully covered by that fuel needs no purchases.

    At each station, buy enough to reach the first cheaper station within range.
    If none exists, fill up, but never buy more than needed for the destination.
    Stations passed without buying fuel are omitted from the returned stops.
    Money is rounded to cents per purchase; fuel quantities retain precision.
    """
    distance = _distance(route.distance_miles, 'route distance')
    initial_fuel = _decimal(starting_fuel_gallons, 'starting fuel')
    if not 0 <= initial_fuel <= TANK_CAPACITY_GALLONS:
        raise ValueError('Starting fuel must be between 0 and 50 gallons.')

    stations = _prepare_stations(candidates, distance)

    fuel, position = initial_fuel, Decimal('0')
    stops = []
    for index, (station_position, price, station) in enumerate(stations):
        required = (station_position - position) / MILES_PER_GALLON
        if required > fuel:
            raise InsufficientFuelCoverage(
                position, station_position, fuel * MILES_PER_GALLON
            )
        fuel -= required
        position = station_position

        # The destination requires no fuel purchase, so cap the target there.
        target_fuel = min(
            TANK_CAPACITY_GALLONS, (distance - position) / MILES_PER_GALLON
        )
        for next_position, next_price, _ in stations[index + 1 :]:
            if next_position - position > MAX_RANGE_MILES:
                break
            if next_price < price:
                target_fuel = (next_position - position) / MILES_PER_GALLON
                break

        gallons = max(Decimal('0'), target_fuel - fuel)
        if gallons > 0:
            cost = (gallons * price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            stops.append(
                FuelStop(station, position, fuel, gallons, fuel + gallons, price, cost)
            )
            fuel += gallons

    required = (distance - position) / MILES_PER_GALLON
    if required > fuel:
        raise InsufficientFuelCoverage(position, distance, fuel * MILES_PER_GALLON)
    fuel -= required
    return FuelPlan(
        stops=tuple(stops),
        starting_fuel_gallons=initial_fuel,
        fuel_used_gallons=distance / MILES_PER_GALLON,
        fuel_remaining_gallons=fuel,
        total_gallons_purchased=sum(
            (stop.gallons_to_buy for stop in stops), Decimal('0')
        ),
        total_purchase_cost=sum(
            (stop.purchase_cost for stop in stops), Decimal('0.00')
        ),
    )
