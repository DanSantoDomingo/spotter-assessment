"""Resolve names or coordinate pairs to US endpoints, caching verified results."""

import re
import unicodedata
from contextlib import nullcontext
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import caches

from .cache import cache_key
from .routes import ORSRoutingClient, _number, _point

# fmt: off
US_STATE_CODES = {
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA', 'HI', 'ID',
    'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD', 'MA', 'MI', 'MN', 'MS',
    'MO', 'MT', 'NE', 'NV', 'NH', 'NJ', 'NM', 'NY', 'NC', 'ND', 'OH', 'OK',
    'OR', 'PA', 'RI', 'SC', 'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV',
    'WI', 'WY', 'DC',
}
# fmt: on


class LocationError(ValueError):
    def __init__(self, message, *, code='location_not_found', candidates=None):
        super().__init__(message)
        self.code = code
        self.candidates = candidates or []


@dataclass(frozen=True)
class ResolvedLocation:
    label: str
    coordinates: tuple[float, float]


def validate_location(value):
    if isinstance(value, str):
        text = ' '.join(unicodedata.normalize('NFKC', value).split())
        text = re.sub(r'\s*,\s*', ', ', text)
        if not 2 <= len(text) <= 300:
            raise ValueError('Place names/addresses must contain 2 to 300 characters.')
        return text
    return _point(value)


def _match_place(feature, *, city, state):
    props = feature['properties']
    if props.get('country_a') not in ('US', 'USA'):
        return None
    if state and props.get('region_a', '').upper() != state:
        return None
    if city and props.get('name', '').casefold() != city.casefold():
        return None
    if props.get('layer') not in (
        'venue',
        'address',
        'locality',
        'borough',
        'neighbourhood',
    ):
        return None
    confidence = _number(props.get('confidence'))
    if not 0.9 <= confidence <= 1 or props.get('match_type') == 'fallback':
        return None
    if feature['geometry']['type'] != 'Point':
        return None
    point = _point(feature['geometry']['coordinates'])
    label = props.get('label')
    if not isinstance(label, str) or not label:
        return None
    return ResolvedLocation(label, point)


def _choose_place(features, *, city=None, state=None):
    matches = {}
    for feature in features:
        try:
            match = _match_place(feature, city=city, state=state)
            if match is not None:
                matches[match.coordinates] = match
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    if not matches:
        raise LocationError(
            'No confident US location matched. '
            'Include the city and state or a full address.'
        )
    if len(matches) > 1:
        candidates = [
            {'label': item.label, 'coordinates': item.coordinates}
            for item in matches.values()
        ]
        raise LocationError(
            'Several US locations matched. '
            'Supply a more specific address or coordinates.',
            code='ambiguous_location',
            candidates=candidates,
        )
    return next(iter(matches.values()))


def resolve_location(value, *, client=None):
    """Return (resolved_location, cache_hit); a cache miss makes one ORS call.

    Coordinate inputs use a country-layer lookup without a US search bias.
    Keep the submitted point, not the country's centroid returned by Pelias.
    """
    value = validate_location(value)
    is_text = isinstance(value, str)
    key = cache_key(
        'place-v2' if is_text else 'country', value.casefold() if is_text else value
    )
    cache = caches['routing']
    result = cache.get(key)
    if result is not None:
        return result, True
    if is_text:
        params = {
            'text': value,
            'country': 'USA',
            'size': 10,
            'layers': 'venue,address,locality,borough,neighbourhood',
        }
        city = state = None
        city_state = re.fullmatch(
            r'([^,\d]+), ([A-Za-z]{2})(?:, (?:USA|US|United States))?', value, re.I
        )
        if city_state and city_state[2].upper() in US_STATE_CODES:
            city, state = city_state[1], city_state[2].upper()
            # Administrative polygon centroids may land far from a road (for
            # example El Paso). Use the populated-place gazetteer for City, ST,
            # and require exact city/state evidence, not just confidence=1.
            params.update(sources='geonames', layers='locality,borough,neighbourhood')
        with nullcontext(client) if client is not None else ORSRoutingClient() as ors:
            features = ors.search_location(**params)
        result = _choose_place(features, city=city, state=state)
    else:
        with nullcontext(client) if client is not None else ORSRoutingClient() as ors:
            features = ors.reverse_location(*value)
        try:
            props = features[0]['properties']
            is_us = props.get('layer') == 'country' and props.get('country_a') in (
                'US',
                'USA',
            )
        except (IndexError, KeyError, TypeError, AttributeError):
            is_us = False
        if not is_us:
            raise LocationError(
                'Coordinates must resolve to a location within the USA.',
                code='outside_usa',
            )
        result = ResolvedLocation(f'{value[0]}, {value[1]}', value)
    cache.set(key, result, timeout=settings.GEOCODING_CACHE_SECONDS)
    return result, False
