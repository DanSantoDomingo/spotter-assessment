"""Match CSV stations to coordinates on their chains' public location pages."""

import json
import re
import time
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx
from django.core.management.base import CommandError

from .loves_locations import (
    LOVES_NAME_PATTERN,
    LocationDataParser,
    _documents,
    lookup_loves,
)

PATTERNS = {
    'loves': LOVES_NAME_PATTERN,
    'pilot': r'^(pilot\b|flying\s*j\b).*#\s*\d+\b',
    'quiktrip': r'^quiktrip\b.*#\s*\d+\b',
    'ta-petro': r'^(petro\b|ta\b|travelcenters\s+of\s+america\b).*#\s*\d+\b',
}


def normalize(value):
    return re.sub(r'[^a-z0-9]', '', str(value or '').casefold())


def slug(value):
    return re.sub(r'[^a-z0-9]+', '-', value.casefold()).strip('-')


def provider_for(name):
    return next(
        (key for key, pattern in PATTERNS.items() if re.search(pattern, name, re.I)),
        None,
    )


def store_matches(name, station):
    expected = re.search(r'#\s*(\d+)\b', station.truckstop_name)
    actual = re.search(r'#\s*(\d+)\b', name)
    if not expected or not actual or int(expected[1]) != int(actual[1]):
        return False
    # Pilot/Flying J and TA/Petro have distinct names even within one directory.
    for brand in (r'pilot\b', r'flying\s*j\b', r'quiktrip\b', r'petro\b'):
        if re.match(brand, station.truckstop_name, re.I):
            return bool(re.match(brand, name, re.I))
    return bool(re.match(r'(?:ta\b|travelcenters\s+of\s+america\b)', name, re.I))


def address_matches(address, station):
    return (
        isinstance(address, dict)
        and address.get('addressCountry', 'US') in ('US', 'USA')
        and normalize(address.get('addressRegion')) == normalize(station.state)
        and normalize(address.get('addressLocality')) == normalize(station.city)
    )


def coordinates(geo):
    try:
        values = [geo[key] for key in ('latitude', 'longitude')]
        if any(isinstance(value, bool) for value in values):
            return None
        lat, lon = (Decimal(str(value)) for value in values)
        if (
            not lat.is_finite()
            or not lon.is_finite()
            or not (-90 <= lat <= 90 and -180 <= lon <= 180)
        ):
            return None
        return lat.quantize(Decimal('.000001')), lon.quantize(Decimal('.000001'))
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None


class PageParser(HTMLParser):
    """Collect directory links and the location's microdata (Pilot's format)."""

    def __init__(self):
        super().__init__()
        self.links = []
        self.properties = defaultdict(set)
        self.headings = []
        self.capture = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        prop = attrs.get('itemprop')
        if tag == 'a':
            self.capture.append((tag, 'link', attrs.get('href', ''), []))
        elif tag == 'h1':
            self.capture.append((tag, 'heading', '', []))
        elif prop in (
            'addressRegion',
            'addressCountry',
            'addressLocality',
            'latitude',
            'longitude',
        ):
            if 'content' in attrs:
                self.properties[prop].add(attrs['content'])
            else:
                self.capture.append((tag, 'property', prop, []))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data):
        for _, _, _, chunks in self.capture:
            chunks.append(data)

    def handle_endtag(self, tag):
        for index in range(len(self.capture) - 1, -1, -1):
            end_tag, kind, key, chunks = self.capture[index]
            if end_tag != tag:
                continue
            self.capture.pop(index)
            value = ' '.join(chunks).strip()
            if kind == 'link':
                self.links.append((key, value))
            elif kind == 'heading':
                self.headings.append(value)
            else:
                self.properties[key].add(value)
            break


def _pilot_coordinates(html, station):
    parser = PageParser()
    parser.feed(html)
    fields = parser.properties
    required = (
        'addressLocality',
        'addressRegion',
        'addressCountry',
        'latitude',
        'longitude',
    )
    if any(store_matches(name, station) for name in parser.headings) and all(
        len(fields[key]) == 1 for key in required
    ):
        values = {key: next(iter(fields[key])) for key in required}
        if address_matches(values, station):
            return coordinates(values)
    return None


def _page_documents(html):
    parser = LocationDataParser()
    parser.feed(html)
    documents = []
    for block in parser.blocks:
        try:
            documents.extend(_documents(json.loads(block)))
        except ValueError:
            continue
    return documents


def _credential_coordinates(document, documents):
    # QT's second JSON-LD block must identify the same business and address.
    matches = set()
    for other in documents:
        subject = other.get('credentialSubject')
        if not isinstance(subject, dict):
            continue
        if (
            subject.get('name') == document.get('name')
            and subject.get('address') == document.get('address')
            and isinstance(document.get('url'), str)
            and isinstance(subject.get('url'), str)
            and subject['url'].split('?')[0].rstrip('/')
            == document['url'].split('?')[0].rstrip('/')
        ):
            matches.add(coordinates(subject.get('geo')))
    return matches


def page_coordinates(html, station, provider):
    if provider == 'pilot':
        point = _pilot_coordinates(html, station)
        return {point} if point is not None else set()
    matches = set()
    documents = _page_documents(html)
    for document in documents:
        name = document.get('name', '')
        if not isinstance(name, str):
            continue
        if provider == 'quiktrip':
            # QuikTrip puts its store number in the business description.
            description = document.get('description', '')
            found = re.match(
                r'Welcome to (QuikTrip\s*#\s*\d+)\b', str(description), re.I
            )
            name = found[1] if found else ''
        if not store_matches(name, station) or not address_matches(
            document.get('address'), station
        ):
            continue
        matches.add(coordinates(document.get('geo')))
        if provider == 'quiktrip':
            matches.update(_credential_coordinates(document, documents))
    matches.discard(None)
    return matches


def _directory_links(html, base, directory, provider, station):
    parser = PageParser()
    parser.feed(html)
    city = slug(station.city)
    candidates = set()
    for href, label in parser.links:
        url = urljoin(base, href).split('?')[0].split('#')[0]
        if urlsplit(url).netloc != urlsplit(directory).netloc or not url.startswith(
            directory.rstrip('/') + '/'
        ):
            continue
        if provider == 'pilot' and not store_matches(label, station):
            continue
        if provider == 'ta-petro':
            location_slug = urlsplit(url).path.rstrip('/').split('/')[-1]
            brand = (
                'petro' if station.truckstop_name.upper().startswith('PETRO') else 'ta'
            )
            if not location_slug.startswith(brand + '-') or not re.search(
                r'(?:^|-)' + re.escape(city) + r'(?:-|$)', location_slug
            ):
                continue
        candidates.add(url)
    return candidates


class OfficialLocations:
    """One batch's HTTP cache and pacing; no API key is required."""

    def __init__(self, client, delay=1):
        self.client = client
        self.delay = delay
        self.cache = {}
        self.last_request = None

    def get(self, url):
        if url in self.cache:
            return self.cache[url]
        if self.last_request is not None:
            time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
        try:
            response = self.client.get(url)
        except httpx.RequestError:
            raise CommandError(
                'Official location request failed. Rerun to resume.'
            ) from None
        finally:
            self.last_request = time.monotonic()
        if response.status_code == 429:
            raise CommandError(
                'Official location website rate limit reached. Wait before resuming.'
            )
        if response.status_code != 404 and not 200 <= response.status_code < 300:
            raise CommandError(
                f'Official location website returned HTTP {response.status_code}; '
                'lookup stopped.'
            )
        self.cache[url] = response
        return response

    def lookup(self, station):
        provider = provider_for(station.truckstop_name)
        if provider == 'loves':
            return lookup_loves(self, station)
        state, city = slug(station.state), slug(station.city)
        if provider == 'pilot':
            directory = f'https://locations.pilotflyingj.com/us/{state}/{city}'
        elif provider == 'quiktrip':
            directory = f'https://locations.quiktrip.com/{state}/{city}'
        elif provider == 'ta-petro':
            directory = f'https://www.ta-petro.com/location/{state}/'
        else:
            return None, 'No official location adapter for this chain'
        response = self.get(directory)
        if response.status_code == 404:
            return None, f'Official city/state directory not found: {directory}'
        # Some cities have only one store and redirect directly to its page.
        matches = page_coordinates(response.text, station, provider)
        candidates = _directory_links(
            response.text,
            str(response.url),
            directory,
            provider,
            station,
        )
        for url in sorted(candidates):
            response = self.get(url)
            if response.status_code != 404:
                matches.update(page_coordinates(response.text, station, provider))
        if len(matches) == 1:
            return (
                matches.pop(),
                f'Official {provider} store number, city and state matched',
            )
        return (
            None,
            'Official pages have no unique coordinates matching store number, '
            'city and state; review manually',
        )
