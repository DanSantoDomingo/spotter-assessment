"""Read station coordinates published in Love's official location pages."""

import json
import re
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

import httpx
from django.core.management.base import CommandError

LOVES_NAME_PATTERN = r"^love['\u2019]?s\b.*#\s*\d+\b"


class LocationDataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.blocks = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if (
            tag == 'script'
            and dict(attrs).get('type', '').lower() == 'application/ld+json'
        ):
            self.current = []

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self.current is not None:
            self.blocks.append(''.join(self.current))
            self.current = None


def _documents(value):
    if isinstance(value, list):
        for item in value:
            yield from _documents(item)
    elif isinstance(value, dict):
        yield value
        yield from _documents(value.get('@graph', []))


def _clean_city(text):
    return re.sub(r'[^a-z0-9]', '', text.casefold())


def _read_coordinates(geo):
    if any(isinstance(geo[key], bool) for key in ('latitude', 'longitude')):
        return None
    latitude, longitude = Decimal(str(geo['latitude'])), Decimal(str(geo['longitude']))
    if not latitude.is_finite() or not longitude.is_finite():
        return None
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        return None
    precision = Decimal('0.000001')
    return latitude.quantize(precision), longitude.quantize(precision)


def _document_coordinates(document, station, store_number):
    name = document.get('name', '')
    store = re.search(r'\bstore\s*#\s*(\d+)\b', name, re.I)
    if not re.match(r"^love['\u2019]?s\b", name, re.I) or not store:
        return None
    if int(store.group(1)) != store_number:
        return None
    address = document['address']
    if address.get('addressCountry') not in ('US', 'USA'):
        return None
    if address.get('addressRegion', '').upper() != station.state.upper():
        return None
    if _clean_city(address['addressLocality']) != _clean_city(station.city):
        return None
    return _read_coordinates(document['geo'])


def coordinates_from_page(html, station, store_number):
    parser = LocationDataParser()
    parser.feed(html)
    matches = set()
    for block in parser.blocks:
        try:
            documents = json.loads(block)
        except ValueError:
            continue
        for document in _documents(documents):
            try:
                point = _document_coordinates(document, station, store_number)
                if point is not None:
                    matches.add(point)
            except (KeyError, TypeError, AttributeError, ValueError, InvalidOperation):
                continue
    if len(matches) == 1:
        return (
            matches.pop(),
            f"Official Love's store #{store_number}; city and state matched",
        )
    return (
        None,
        'Official page has no unique coordinates matching store number, city, '
        'and state; review manually',
    )


def lookup_loves(client, station):
    if not re.search(LOVES_NAME_PATTERN, station.truckstop_name, re.I):
        return None, "Not a supported Love's store name"
    store_number = int(re.search(r'#\s*(\d+)', station.truckstop_name).group(1))
    try:
        response = client.get(f'https://www.loves.com/locations/{store_number}')
    except httpx.TimeoutException:
        raise CommandError(
            "Love's location lookup timed out. Rerun to resume."
        ) from None
    except httpx.RequestError:
        raise CommandError(
            "Could not connect to Love's location website. Rerun to resume."
        ) from None
    if response.status_code == 404:
        return (
            None,
            f"Official page for Love's #{store_number} was not found; review manually",
        )
    if response.status_code == 429:
        raise CommandError("Love's website rate limit reached. Wait before resuming.")
    if not 200 <= response.status_code < 300:
        raise CommandError(
            f"Love's website returned HTTP {response.status_code}; lookup stopped."
        )
    return coordinates_from_page(response.text, station, store_number)
