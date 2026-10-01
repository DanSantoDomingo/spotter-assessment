import csv
import math
import os
import time
from pathlib import Path

import httpx
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from fuels.models import FuelStation

from .providers.bulk_locations import BULK_PATTERN, BulkLocations
from .providers.official_locations import PATTERNS, OfficialLocations
from .providers.ors_locations import choose_coordinates, lookup_ors

US_STATES = set(
    [
        'AL',
        'AK',
        'AZ',
        'AR',
        'CA',
        'CO',
        'CT',
        'DE',
        'FL',
        'GA',
        'HI',
        'ID',
        'IL',
        'IN',
        'IA',
        'KS',
        'KY',
        'LA',
        'ME',
        'MD',
        'MA',
        'MI',
        'MN',
        'MS',
        'MO',
        'MT',
        'NE',
        'NV',
        'NH',
        'NJ',
        'NM',
        'NY',
        'NC',
        'ND',
        'OH',
        'OK',
        'OR',
        'PA',
        'RI',
        'SC',
        'SD',
        'TN',
        'TX',
        'UT',
        'VT',
        'VA',
        'WA',
        'WV',
        'WI',
        'WY',
        'DC',
    ]
)
MISSING_COORDINATES = Q(latitude__isnull=True) | Q(longitude__isnull=True)
REPORT_FIELDS = ['station_id', 'opis_id', 'name', 'outcome', 'detail']


def _validate_options(options):
    if options['limit'] < 1 or options['after_id'] < 0:
        raise CommandError(
            '--limit must be positive and --after-id must be nonnegative.'
        )
    if not math.isfinite(options['delay']) or options['delay'] < 0:
        raise CommandError('--delay must be a finite, nonnegative number.')
    if (
        not math.isfinite(options['min_confidence'])
        or not 0 <= options['min_confidence'] <= 1
    ):
        raise CommandError('--min-confidence must be between zero and one.')
    if options['provider'] == 'bulk' and options['state'] != 'TX':
        raise CommandError('The bulk provider currently requires --state TX.')


def _station_batch(options):
    stations = FuelStation.objects.filter(
        MISSING_COORDINATES, state__in=US_STATES, pk__gt=options['after_id']
    ).order_by('pk')
    if options['state']:
        stations = stations.filter(state=options['state'])
    if options['opis_id'] is not None:
        stations = stations.filter(opis_truckstop_id=options['opis_id'])
    if options['provider'] == 'bulk':
        stations = stations.filter(truckstop_name__iregex=BULK_PATTERN)
    elif options['provider'] != 'ors':
        patterns = (
            PATTERNS.values()
            if options['provider'] == 'official'
            else [PATTERNS[options['provider']]]
        )
        stations = stations.filter(
            truckstop_name__iregex='|'.join(f'({pattern})' for pattern in patterns)
        )
    return list(stations[: options['limit']])


def _request_headers(options):
    headers = {'User-Agent': 'SpotterStationImport/0.1'}
    if options['provider'] == 'ors':
        api_key = getattr(settings, 'ORS_API_KEY', '') or os.environ.get(
            'ORS_API_KEY', ''
        )
        if not isinstance(api_key, str) or not api_key.strip():
            raise CommandError(
                'Set ORS_API_KEY in your environment before running this command.'
            )
        headers.update({'Authorization': api_key.strip(), 'Accept': 'application/json'})

    return headers


class Command(BaseCommand):
    help = (
        'Populate missing coordinates using ORS, official chain pages, '
        'or cached Texas bulk catalogs.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--provider',
            choices=('ors', 'official', 'bulk', *PATTERNS),
            default='ors',
            help=(
                "Use 'bulk' for cached Texas catalogs, 'official' for supported "
                "chain pages, or select one chain. Only 'ors' needs an API key."
            ),
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=100,
            help='Maximum station lookups (default: 100).',
        )
        parser.add_argument(
            '--delay',
            type=float,
            default=1.0,
            help='Seconds between API requests (default: 1).',
        )
        parser.add_argument(
            '--state',
            type=str.upper,
            choices=sorted(US_STATES),
            help='Only process this US state/DC.',
        )
        parser.add_argument(
            '--opis-id', type=int, help='Only process this OPIS station ID.'
        )
        parser.add_argument(
            '--after-id',
            type=int,
            default=0,
            help='Skip database primary keys up to this ID.',
        )
        parser.add_argument(
            '--min-confidence',
            type=float,
            default=0.9,
            help='Minimum provider confidence (default: 0.9).',
        )
        parser.add_argument(
            '--report', type=Path, help='Append outcomes to this CSV report.'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report matches without changing station coordinates.',
        )
        parser.add_argument(
            '--cache-dir',
            type=Path,
            default=Path(settings.BASE_DIR) / '.geocoding-cache',
            help='Directory for reusable bulk source snapshots.',
        )
        parser.add_argument(
            '--refresh-cache',
            action='store_true',
            help='Download fresh bulk snapshots instead of using the cache.',
        )

    def handle(self, *_args, **options):
        _validate_options(options)
        batch = _station_batch(options)
        if not batch:
            self.stdout.write(
                'No stations with missing coordinates match these filters.'
            )
            return

        headers = _request_headers(options)

        report_file = None
        try:
            writer = None
            if options['report']:
                report_file = options['report'].open('a+', encoding='utf-8', newline='')
                report_file.seek(0)
                header = next(csv.reader(report_file), None)
                if header is not None and header != REPORT_FIELDS:
                    raise CommandError(
                        'Report file has an incompatible CSV header; use another path.'
                    )
                report_file.seek(0, 2)
                writer = csv.DictWriter(report_file, fieldnames=REPORT_FIELDS)
                if header is None:
                    writer.writeheader()
                    report_file.flush()

            with httpx.Client(
                headers=headers,
                timeout=httpx.Timeout(15.0, connect=5.0),
                follow_redirects=options['provider'] != 'ors',
                max_redirects=5,
            ) as client:
                self.populate(client, batch, options, writer, report_file)
        except OSError:
            raise CommandError(
                'Could not read or write the geocoding report/cache.'
            ) from None
        finally:
            if report_file is not None:
                report_file.close()

    def populate(self, client, batch, options, writer, report_file):
        saved = unresolved = attempted = matched = 0
        last_id = options['after_id']
        official = OfficialLocations(client, delay=options['delay'])
        bulk = BulkLocations(
            client,
            options['cache_dir'],
            delay=options['delay'],
            refresh=options['refresh_cache'],
        )
        try:
            for station in batch:
                if attempted and options['provider'] == 'ors':
                    time.sleep(options['delay'])
                attempted += 1
                try:
                    if options['provider'] == 'bulk':
                        coordinates, reason = bulk.lookup(station)
                    elif options['provider'] != 'ors':
                        coordinates, reason = official.lookup(station)
                    else:
                        features = lookup_ors(client, station)
                        coordinates, reason = choose_coordinates(
                            features, station, options['min_confidence']
                        )
                except CommandError as exc:
                    self.record(writer, report_file, station, 'error', str(exc))
                    raise
                if coordinates is None:
                    unresolved += 1
                    self.stderr.write(f'OPIS {station.opis_truckstop_id}: {reason}')
                    self.record(writer, report_file, station, 'unresolved', reason)
                else:
                    latitude, longitude = coordinates
                    matched += 1
                    # Do not overwrite a concurrent lookup or a changed address.
                    updated = (
                        0
                        if options['dry_run']
                        else FuelStation.objects.filter(
                            MISSING_COORDINATES,
                            pk=station.pk,
                            truckstop_name=station.truckstop_name,
                            address=station.address,
                            city=station.city,
                            state=station.state,
                        ).update(latitude=latitude, longitude=longitude)
                    )
                    saved += updated
                    outcome = (
                        'matched'
                        if options['dry_run']
                        else 'saved'
                        if updated
                        else 'skipped'
                    )
                    detail = (
                        f'{latitude}, {longitude}'
                        if updated or options['dry_run']
                        else 'Station changed or was populated concurrently'
                    )
                    if (updated or options['dry_run']) and options['provider'] != 'ors':
                        detail += f'; {reason}'
                    self.record(writer, report_file, station, outcome, detail)
                last_id = station.pk
                self.stdout.write(
                    f'[{attempted}/{len(batch)}] OPIS {station.opis_truckstop_id}: '
                    f'{"unresolved" if coordinates is None else outcome}'
                )
        except KeyboardInterrupt:
            raise CommandError(
                'Interrupted. Previously saved coordinates are preserved.'
            ) from None
        finally:
            self.stdout.write(
                f'Attempted {attempted}; matched {matched}; saved {saved}; '
                f'unresolved {unresolved}. '
                f'Last completed database ID: {last_id}.'
            )

    def record(self, writer, report_file, station, outcome, detail):
        if writer is not None:
            writer.writerow(
                {
                    'station_id': station.pk,
                    'opis_id': station.opis_truckstop_id,
                    'name': station.truckstop_name,
                    'outcome': outcome,
                    'detail': detail,
                }
            )
            report_file.flush()
