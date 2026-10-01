"""OpenRouteService geocoding requests and conservative station matching."""

import math
import re
from collections import Counter
from decimal import Decimal, InvalidOperation

from django.core.management.base import CommandError

from fuels.services.routes import RoutingError


def normalize(value):
    return re.sub(r'[^a-z0-9]', '', str(value or '').casefold())


class _RejectedCandidate(Exception):
    """A well-formed candidate that does not meet the station matching rules."""


def _validate_locality(properties, station):
    if properties.get('country_a') not in ('USA', 'US'):
        country = properties.get('country_a') or 'missing'
        raise _RejectedCandidate(f'wrong country ({country})')
    if properties.get('region_a', '').upper() != station.state:
        state = properties.get('region_a') or 'missing'
        raise _RejectedCandidate(f'wrong state ({state}; expected {station.state})')
    if normalize(properties.get('locality')) != normalize(station.city):
        city = properties.get('locality') or 'missing'
        raise _RejectedCandidate(f'wrong city ({city}; expected {station.city})')


def _validate_confidence(properties, min_confidence):
    confidence = properties.get('confidence')
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise _RejectedCandidate('missing or invalid confidence')
    if not math.isfinite(confidence) or not min_confidence <= confidence <= 1:
        raise _RejectedCandidate('confidence outside accepted range')


def _validate_identity(properties, station):
    layer = properties.get('layer')
    if layer == 'venue':
        if normalize(properties.get('name')) != normalize(station.truckstop_name):
            raise _RejectedCandidate('venue name/store number does not match')
    elif layer == 'address':
        house, street = properties.get('housenumber'), properties.get('street')
        if not house or not street:
            raise _RejectedCandidate('missing house number or street')
        if normalize(f'{house} {street}') != normalize(station.address):
            raise _RejectedCandidate('street address does not match')
    else:
        raise _RejectedCandidate('coarse result (not a venue or street address)')


def _read_coordinates(geometry):
    longitude, latitude = geometry['coordinates']
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        for value in (longitude, latitude)
    ):
        raise _RejectedCandidate('invalid coordinates')
    longitude, latitude = Decimal(str(longitude)), Decimal(str(latitude))
    if not longitude.is_finite() or not latitude.is_finite():
        raise _RejectedCandidate('nonfinite coordinates')
    if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
        raise _RejectedCandidate('coordinates outside valid ranges')
    precision = Decimal('0.000001')
    return latitude.quantize(precision), longitude.quantize(precision)


def _candidate_coordinates(feature, station, min_confidence):
    properties, geometry = feature['properties'], feature['geometry']
    if geometry['type'] != 'Point':
        raise _RejectedCandidate('non-point geometry')
    _validate_locality(properties, station)
    _validate_confidence(properties, min_confidence)
    _validate_identity(properties, station)
    return _read_coordinates(geometry)


def choose_coordinates(features, station, min_confidence):
    """Require matching locality and venue name or full address, plus confidence."""
    matches = set()
    rejections = Counter()
    if not features:
        return None, 'Geocoder returned no candidates; review manually'
    for feature in features:
        try:
            matches.add(_candidate_coordinates(feature, station, min_confidence))
        except _RejectedCandidate as error:
            rejections[str(error)] += 1
        except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation):
            rejections['malformed candidate'] += 1
    if len(matches) == 1:
        return matches.pop(), 'Unique location matched'
    if len(matches) > 1:
        return None, 'Multiple locations matched; review manually'
    reasons = '; '.join(
        f'{count} {reason}' for reason, count in rejections.most_common()
    )
    return None, f'No matching station/address: {reasons}; review manually'


def lookup_ors(client, station):
    query = f'{station.truckstop_name}, {station.city}, {station.state}'
    try:
        return client.search_location(query, layers='venue,address', size=5)
    except RoutingError as error:
        raise CommandError(f'{error} Rerun after resolving the error.') from error
