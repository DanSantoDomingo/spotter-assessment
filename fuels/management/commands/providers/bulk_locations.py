"""Texas station matching from public bulk catalogs, with auditable local copies.

All the Places refs are source-specific identifiers, not OPIS IDs. Only adapters
whose store-number semantics have been verified participate in automatic matches.
"""

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

import httpx
from django.core.management.base import CommandError

from .official_locations import coordinates, normalize

BULK_PATTERN = (
    r'^(circle\s*k\b|7[- ]?eleven\b|stripes\b|cefco\b|'
    r"toot['\u2019]?\s*n\s*totum\b|road\s*rangers?\b).*#\s*\d+\b"
)
ATP_ROOT = 'https://data.alltheplaces.xyz/runs/latest/output/'
CIRCLE_URL = (
    'https://api.circlek.com/us/ngrp-store-locator/v1/stations'
    '?bottomRightLongitude=-93.5&bottomRightLatitude=25.8'
    '&topLeftLongitude=-106.7&topLeftLatitude=36.6'
    '&maxResults=2000&country=US&brand=CIRCLEK'
)
TOOT_URL = 'https://www.tootntotum.com/locations'
ROAD_CODES_URL = 'https://www.roadrangerusa.com/locations-amenities/station-codes'
ROAD_MAP_URL = 'https://www.roadrangerusa.com/locations-amenities/find-a-road-ranger'


def chain_for(name):
    for key, pattern in (
        ('circle_k', r'^circle\s*k\b'),
        ('seven_eleven_ca_us', r'^(7[- ]?eleven|stripes)\b'),
        ('cefco_us', r'^cefco\b'),
        ('toot', r"^toot['\u2019]?\s*n\s*totum\b"),
        ('road', r'^road\s*rangers?\b'),
    ):
        if re.search(pattern, name, re.I):
            return key
    return None


@dataclass(frozen=True)
class Location:
    number: int
    city: str
    state: str
    point: tuple
    source: str
    opis: int | None = None


def read_json(text):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        raise CommandError(
            'Location source returned invalid JSON; no match attempted.'
        ) from None


class CatalogHTML(HTMLParser):
    """Extract scoped Road Ranger rows and Toot'n Totum's embedded map data."""

    def __init__(self):
        super().__init__()
        self.maps = []
        self.rows = []
        self.active = None
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'data-locations' in attrs:
            self.maps.append(read_json(attrs['data-locations']))
        if tag == 'div':
            self.depth += 1
            classes = attrs.get('class', '').split()
            if 'store-block' in classes or 'geolocation-location' in classes:
                self.active = (self.depth, attrs, [])

    def handle_data(self, text):
        if self.active is not None:
            self.active[2].append(text)

    def handle_endtag(self, tag):
        if tag == 'div':
            if self.active is not None and self.active[0] == self.depth:
                _, attrs, text = self.active
                self.rows.append((attrs, ' '.join(' '.join(text).split())))
                self.active = None
            self.depth -= 1


def _atp_identity(props, spider):
    brand = normalize(props.get('brand'))
    city = props.get('addr:city', '')
    if spider == 'seven_eleven_ca_us':
        # Only the branded shop entry establishes chain identity.
        if brand not in ('7eleven', 'stripes'):
            return '', ''
        return str(props.get('ref', '')).removesuffix('_SHOP'), city
    if brand != 'cefco':
        return '', ''
    if not city:
        # CEFCO exports 'street, city, TX ZIP' as addr:full.
        address = str(props.get('addr:full', '')).split(',')
        if len(address) == 3 and re.fullmatch(
            r'\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\s*', address[-1]
        ):
            if address[-1].strip()[:2] != props.get('addr:state'):
                return '', ''
            city = address[-2].strip()
    return str(props.get('ref', '')), city


def atp_locations(data, spider, source):
    if (
        not isinstance(data, dict)
        or data.get('type') != 'FeatureCollection'
        or not isinstance(data.get('features'), list)
    ):
        raise CommandError('Bulk catalog is not a GeoJSON FeatureCollection.')
    # A successful download may be an old snapshot. Keep its date visible.
    attributes = data.get('dataset_attributes', {})
    if attributes.get('@spider') != spider:
        raise CommandError('Bulk catalog has an unexpected source identifier.')
    dated_source = (
        f'{source} (collected {attributes.get("spider:collection_time", "unknown")})'
    )
    result = []
    for feature in data['features']:
        try:
            props, geometry = feature['properties'], feature['geometry']
            if (
                props.get('@spider') != spider
                or props.get('addr:country') != 'US'
                or geometry['type'] != 'Point'
            ):
                continue
            ref, city = _atp_identity(props, spider)
            lon, lat = geometry['coordinates']
            point = coordinates({'latitude': lat, 'longitude': lon})
            if ref.isdigit() and city and point:
                result.append(
                    Location(
                        int(ref), city, props.get('addr:state', ''), point, dated_source
                    )
                )
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return result


def circle_locations(data, source):
    if not isinstance(data, list):
        raise CommandError('Circle K returned an invalid catalog.')
    if len(data) >= 2000:
        raise CommandError(
            'Circle K result limit reached; split the search area before importing.'
        )
    result = []
    for row in data:
        try:
            address = row['address']
            point = coordinates(row)
            if (
                row.get('brand') == 'CIRCLEK'
                and address.get('country') == 'US'
                and str(row['id']).isdigit()
                and point
            ):
                result.append(
                    Location(
                        int(row['id']), address['city'], address['state'], point, source
                    )
                )
        except (KeyError, TypeError, AttributeError):
            continue
    return result


def toot_locations(html, source):
    parser = CatalogHTML()
    parser.feed(html)
    if not parser.maps:
        raise CommandError(
            "Toot'n Totum map data was not found; its page format may have changed."
        )
    result = []
    for rows in parser.maps:
        if not isinstance(rows, list):
            raise CommandError("Invalid Toot'n Totum map data.")
        for row in rows:
            try:
                match = re.fullmatch(
                    r"Toot['\u2019]?n Totum #\s*(\d+)", row['title'], re.I
                )
                lat, lon = row['position'].split(',')
                point = coordinates({'latitude': lat.strip(), 'longitude': lon.strip()})
                if match and point:
                    result.append(
                        Location(
                            int(match[1]), row['city'], row['state'], point, source
                        )
                    )
            except (KeyError, TypeError, AttributeError, ValueError):
                continue
    return result


def road_locations(codes_html, map_html, source):
    codes, pins = CatalogHTML(), CatalogHTML()
    codes.feed(codes_html)
    pins.feed(map_html)
    if not codes.rows or not pins.rows:
        raise CommandError(
            'Road Ranger directory format changed; could not read store '
            'identities and map pins.'
        )
    points = {}
    for attrs, address in pins.rows:
        point = coordinates(
            {'latitude': attrs.get('data-lat'), 'longitude': attrs.get('data-lng')}
        )
        if point:
            points.setdefault(normalize(address), set()).add(point)
    result = []
    for attrs, text in codes.rows:
        store = re.match(r'Store #\s*(\d+)\s+(.+?)\s+Comdata\b', text)
        opis = re.search(r'\bOPIS\s+(\d+)\b', text)
        if not store or not opis:
            continue
        address = store[2]
        # Match two official pages on an exact address, then retain both store
        # number and OPIS ID. A city alone is never enough to identify a station.
        city_state = re.search(r',\s*([^,]+?)\s+([A-Z]{2})(?:\s+\d{5})?$', address)
        if not city_state or city_state[2] != attrs.get('data-state'):
            continue
        for point in points.get(normalize(address), set()):
            result.append(
                Location(
                    int(store[1]),
                    city_state[1],
                    city_state[2],
                    point,
                    source,
                    int(opis[1]),
                )
            )
    return result


class BulkLocations:
    """Download each source once, cache it on disk, then match locally."""

    def __init__(self, client, cache_dir, delay=1, refresh=False):
        self.client = client
        self.cache_dir = Path(cache_dir)
        self.delay = delay
        self.refresh = refresh
        self.loaded = {}
        self.last_request = None

    def fetch(self, key, url):
        path = self.cache_dir / f'{key}.json'
        if path.exists() and not self.refresh:
            try:
                cached = json.loads(path.read_text(encoding='utf-8'))
                body = cached['body']
                if (
                    cached['requested_url'] != url
                    or cached['sha256'] != hashlib.sha256(body.encode()).hexdigest()
                ):
                    raise ValueError
                return body, cached['source']
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                raise CommandError(
                    f'Invalid location cache {path}. Use --refresh-cache to replace it.'
                ) from None
        if self.last_request is not None:
            time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
        try:
            response = self.client.get(url, timeout=httpx.Timeout(60, connect=10))
        except httpx.RequestError:
            raise CommandError(
                'Bulk location download failed; rerun to resume from cached sources.'
            ) from None
        finally:
            self.last_request = time.monotonic()
        if response.status_code == 429:
            raise CommandError(
                'Location source rate limit reached. Wait before resuming.'
            )
        if response.status_code != 200:
            raise CommandError(
                f'Location source returned HTTP {response.status_code}: {url}'
            )
        body = response.text
        if len(response.content) > 30_000_000:
            raise CommandError('Unexpectedly large location catalog; download stopped.')
        fetched = datetime.now(UTC).isoformat()
        source = f'{response.url} (downloaded {fetched})'
        cached = {
            'requested_url': url,
            'source': source,
            'body': body,
            'sha256': hashlib.sha256(body.encode()).hexdigest(),
        }
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(cached), encoding='utf-8')
        temporary.replace(path)
        return body, source

    def _load_catalog(self, key):
        if key in ('seven_eleven_ca_us', 'cefco_us'):
            body, source = self.fetch(key, ATP_ROOT + key + '.geojson')
            rows = atp_locations(read_json(body), key, source)
        elif key == 'circle_k':
            body, source = self.fetch(key, CIRCLE_URL)
            rows = circle_locations(read_json(body), source)
        elif key == 'toot':
            body, source = self.fetch(key, TOOT_URL)
            rows = toot_locations(body, source)
        else:
            codes, codes_source = self.fetch('road_codes', ROAD_CODES_URL)
            pins, map_source = self.fetch('road_map', ROAD_MAP_URL)
            rows = road_locations(codes, pins, f'{codes_source}; {map_source}')
        if not rows:
            raise CommandError(
                f'No usable locations in {key}; '
                'check the source format before continuing.'
            )
        self.loaded[key] = {}
        for row in rows:
            identity = (row.number, normalize(row.city), normalize(row.state))
            self.loaded[key].setdefault(identity, []).append(row)

    def lookup(self, station):
        if station.state != 'TX':
            return None, 'Bulk provider currently supports Texas only'
        key = chain_for(station.truckstop_name)
        number = re.search(r'#\s*(\d+)\b', station.truckstop_name)
        if not key or not number:
            return None, 'No supported bulk catalog/store number for this station'
        if key not in self.loaded:
            self._load_catalog(key)
        identity = (int(number[1]), normalize(station.city), normalize(station.state))
        candidates = [
            row
            for row in self.loaded[key].get(identity, [])
            if row.opis is None or row.opis == station.opis_truckstop_id
        ]
        points = {row.point for row in candidates}
        if len(points) == 1:
            return (
                points.pop(),
                f'Matched {key} store #{int(number[1])}, city/state; '
                f'{candidates[0].source}',
            )
        if points:
            return None, 'Conflicting coordinates for the same store; review manually'
        return (
            None,
            'No exact store number and city/state match in bulk catalog; '
            'review manually',
        )
