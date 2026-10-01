import json
import re
from copy import deepcopy
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from fuels.management.commands.providers.bulk_locations import (
    BulkLocations,
    atp_locations,
    chain_for,
    toot_locations,
)
from fuels.management.commands.providers.loves_locations import (
    LOVES_NAME_PATTERN,
    coordinates_from_page,
)
from fuels.management.commands.providers.official_locations import (
    OfficialLocations,
    page_coordinates,
)
from fuels.management.commands.providers.ors_locations import choose_coordinates
from fuels.models import FuelStation


def station(name="Love's #12"):
    return SimpleNamespace(
        truckstop_name=name,
        city='Austin',
        state='TX',
        address='123 Main St',
        opis_truckstop_id=12,
    )


def feature():
    return {
        'geometry': {'type': 'Point', 'coordinates': [-97.7, 30.2]},
        'properties': {
            'country_a': 'USA',
            'region_a': 'TX',
            'locality': 'Austin',
            'confidence': 1,
            'layer': 'venue',
            'name': "Love's #12",
        },
    }


def location_document(name):
    return {
        'name': name,
        'address': {
            'addressCountry': 'US',
            'addressRegion': 'TX',
            'addressLocality': 'Austin',
        },
        'geo': {'latitude': 30.2, 'longitude': -97.7},
    }


def html(*documents):
    return ''.join(
        '<script type="application/ld+json">' + json.dumps(item) + '</script>'
        for item in documents
    )


class ProviderMatchingTests(SimpleTestCase):
    def test_ors_preserves_rejection_reasons(self):
        changes = [
            ({'country_a': 'CAN'}, 'wrong country'),
            ({'region_a': 'CA'}, 'wrong state'),
            ({'locality': 'Dallas'}, 'wrong city'),
            ({'confidence': True}, 'missing or invalid confidence'),
            ({'confidence': 0.5}, 'confidence outside accepted range'),
            ({'name': "Love's #99"}, 'venue name/store number does not match'),
            ({'layer': 'locality'}, 'coarse result'),
            ({'layer': 'address'}, 'missing house number or street'),
            (
                {'layer': 'address', 'housenumber': '999', 'street': 'Main St'},
                'street address does not match',
            ),
        ]
        for properties, reason in changes:
            item = feature()
            item['properties'].update(properties)
            with self.subTest(properties=properties):
                point, detail = choose_coordinates([item], station(), 0.9)
                self.assertIsNone(point)
                self.assertIn(reason, detail)

    def test_ors_requires_unique_valid_coordinates(self):
        valid = feature()
        point, _ = choose_coordinates([{}, valid, deepcopy(valid)], station(), 0.9)
        self.assertEqual(point, (Decimal('30.200000'), Decimal('-97.700000')))
        other = deepcopy(valid)
        other['geometry']['coordinates'] = [-97.8, 30.3]
        self.assertIsNone(choose_coordinates([valid, other], station(), 0.9)[0])
        for values in ([True, 30], [200, 30], [float('nan'), 30], [1]):
            other['geometry']['coordinates'] = values
            with self.subTest(values=values):
                self.assertIsNone(choose_coordinates([other], station(), 0.9)[0])

    def test_loves_curly_apostrophes_and_identity_checks(self):
        name = 'Love\u2019s Store #12'
        self.assertIsNotNone(re.search(LOVES_NAME_PATTERN, name, re.I))
        document = location_document(name)
        point, _ = coordinates_from_page(html({'@graph': [document]}), station(), 12)
        self.assertEqual(point, (Decimal('30.200000'), Decimal('-97.700000')))
        for changed in (
            {'name': "Love's Store #99"},
            {'address': {**document['address'], 'addressLocality': 'Dallas'}},
            {'geo': {'latitude': True, 'longitude': -97.7}},
        ):
            with self.subTest(changed=changed):
                self.assertIsNone(
                    coordinates_from_page(
                        html(document | changed),
                        station(),
                        12,
                    )[0]
                )

    def test_quiktrip_credentials_must_belong_to_same_store(self):
        document = location_document('QuikTrip')
        point = document.pop('geo')
        document.update(description='Welcome to QuikTrip #12', url='https://qt/12')
        subject = document | {'geo': point}
        result = page_coordinates(
            html(document, {'credentialSubject': subject}),
            station('QuikTrip #12'),
            'quiktrip',
        )
        self.assertEqual(result, {(Decimal('30.200000'), Decimal('-97.700000'))})
        subject['url'] = 'https://qt/99'
        self.assertEqual(
            page_coordinates(
                html(document, {'credentialSubject': subject}),
                station('QuikTrip #12'),
                'quiktrip',
            ),
            set(),
        )

    def test_pilot_microdata_and_directory_links(self):
        directory = 'https://locations.pilotflyingj.com/us/tx/austin'
        store_page = '<h1>Pilot #12</h1>' + ''.join(
            f'<meta itemprop="{key}" content="{value}">'
            for key, value in {
                'addressLocality': 'Austin',
                'addressRegion': 'TX',
                'addressCountry': 'US',
                'latitude': '30.2',
                'longitude': '-97.7',
            }.items()
        )
        requests = []

        def handler(request):
            requests.append(str(request.url))
            if str(request.url) == directory:
                return httpx.Response(
                    200,
                    text=(
                        f'<a href="{directory}/12">Pilot #12</a>'
                        f'<a href="{directory}/99">Pilot #99</a>'
                        '<a href="https://unrelated.example/12">Pilot #12</a>'
                    ),
                )
            self.assertEqual(str(request.url), directory + '/12')
            return httpx.Response(200, text=store_page)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            point, _ = OfficialLocations(client, delay=0).lookup(station('Pilot #12'))
        self.assertEqual(point, (Decimal('30.200000'), Decimal('-97.700000')))
        self.assertEqual(len(requests), 2)

    def test_atp_cefco_address_fallback_and_state_mismatch(self):
        item = feature()
        item['properties'] = {
            '@spider': 'cefco_us',
            'brand': 'CEFCO',
            'ref': '12',
            'addr:country': 'US',
            'addr:state': 'TX',
            'addr:full': '123 Main St, Austin, TX 78701',
        }
        data = {
            'type': 'FeatureCollection',
            'dataset_attributes': {'@spider': 'cefco_us'},
            'features': [item],
        }
        result = atp_locations(data, 'cefco_us', 'test')
        self.assertEqual([(row.number, row.city) for row in result], [(12, 'Austin')])
        item['properties']['addr:full'] = '123 Main St, Austin, CA 78701'
        self.assertEqual(atp_locations(data, 'cefco_us', 'test'), [])

    def test_bulk_fetch_is_cached_and_ambiguous_stores_are_rejected(self):
        row = {
            'id': 12,
            'brand': 'CIRCLEK',
            'latitude': 30.2,
            'longitude': -97.7,
            'address': {'city': 'Austin', 'state': 'TX', 'country': 'US'},
        }
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=[row])

        with (
            TemporaryDirectory() as directory,
            httpx.Client(transport=httpx.MockTransport(handler)) as client,
        ):
            first = BulkLocations(client, directory, delay=0)
            point, _ = first.lookup(station('Circle K #12'))
            second = BulkLocations(client, directory, delay=0)
            self.assertEqual(second.lookup(station('Circle K #12'))[0], point)
            self.assertEqual(len(requests), 1)
            row['longitude'] = -98
            conflict = httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json=[row, row | {'longitude': -97.7}],
                )
            )
            with httpx.Client(transport=conflict) as other_client:
                third = BulkLocations(other_client, directory, delay=0, refresh=True)
                self.assertIsNone(third.lookup(station('Circle K #12'))[0])

    def test_toot_curly_apostrophe_still_matches(self):
        name = 'Toot\u2019n Totum #12'
        self.assertEqual(chain_for(name), 'toot')
        data = json.dumps(
            [
                {
                    'title': name,
                    'city': 'Austin',
                    'state': 'TX',
                    'position': '30.2, -97.7',
                }
            ]
        )
        result = toot_locations(f"<div data-locations='{data}'></div>", 'test')
        self.assertEqual([row.number for row in result], [12])


@override_settings(ORS_API_KEY='test')
class GeocodingCommandTests(TestCase):
    def setUp(self):
        self.station = FuelStation.objects.create(
            opis_truckstop_id=12,
            truckstop_name="Love's #12",
            city='Austin',
            state='TX',
            address='123 Main St',
            rack_id=1,
            retail_price=Decimal('3'),
        )

    def run_command(self, **options):
        call_command(
            'geocode_fuel_stations',
            state='TX',
            delay=0,
            stdout=StringIO(),
            stderr=StringIO(),
            **options,
        )

    @patch('fuels.management.commands.geocode_fuel_stations.lookup_ors')
    def test_dry_run_and_existing_coordinates_are_preserved(self, lookup):
        lookup.return_value = [feature()]
        self.run_command(dry_run=True)
        self.station.refresh_from_db()
        self.assertIsNone(self.station.latitude)
        self.run_command()
        self.station.refresh_from_db()
        self.assertEqual(self.station.latitude, Decimal('30.200000'))
        self.assertEqual(self.station.retail_price, Decimal('3'))
        self.run_command()
        self.assertEqual(lookup.call_count, 2)

    @patch('fuels.management.commands.geocode_fuel_stations.lookup_ors')
    def test_concurrent_address_edit_is_not_overwritten(self, lookup):
        def change_address(*_args):
            FuelStation.objects.filter(pk=self.station.pk).update(address='Other')
            return [feature()]

        lookup.side_effect = change_address
        self.run_command()
        self.station.refresh_from_db()
        self.assertIsNone(self.station.latitude)
        self.assertEqual(self.station.address, 'Other')

    @patch('fuels.management.commands.geocode_fuel_stations.lookup_ors')
    def test_invalid_options_and_report_fail_before_lookup(self, lookup):
        for options in (
            {'limit': 0},
            {'delay': float('nan')},
            {'min_confidence': 2},
            {'provider': 'bulk', 'state': 'CA'},
        ):
            with self.subTest(options=options), self.assertRaises(CommandError):
                call_command('geocode_fuel_stations', **options)
        with TemporaryDirectory() as directory:
            report = Path(directory) / 'report.csv'
            report.write_text('wrong,header\n', encoding='utf-8')
            with self.assertRaises(CommandError):
                self.run_command(report=report)
        lookup.assert_not_called()
