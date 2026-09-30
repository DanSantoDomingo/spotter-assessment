import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from fuels.models import FuelStation


class Command(BaseCommand):
    help = (
        "Import fuel prices by OPIS ID. Existing stations are updated; "
        "the last CSV row wins when an ID appears more than once."
    )

    columns = {
        "OPIS Truckstop ID": "opis_truckstop_id",
        "Truckstop Name": "truckstop_name",
        "Address": "address",
        "City": "city",
        "State": "state",
        "Rack ID": "rack_id",
        "Retail Price": "retail_price",
    }

    def add_arguments(self, parser):
        parser.add_argument("csv_path", type=Path, help="Path to the fuel prices CSV.")

    def handle(self, *args, **options):
        stations = {}
        row_count = 0

        try:
            with options["csv_path"].open(encoding="utf-8-sig", newline="") as source:
                reader = csv.DictReader(source, strict=True)
                missing = self.columns.keys() - set(reader.fieldnames or [])
                if missing:
                    raise CommandError(f"Missing CSV columns: {', '.join(sorted(missing))}")

                for row in reader:
                    try:
                        if None in row:
                            raise ValueError("Row contains extra CSV values")
                        values = {
                            field: (row[column] or "").strip()
                            for column, field in self.columns.items()
                        }
                        values["opis_truckstop_id"] = int(values["opis_truckstop_id"])
                        values["rack_id"] = int(values["rack_id"])
                        values["retail_price"] = Decimal(values["retail_price"])
                        values["state"] = values["state"].upper()
                        station = FuelStation(**values)
                        station.full_clean(validate_unique=False, validate_constraints=False)
                    except (ValueError, InvalidOperation, ValidationError) as exc:
                        raise CommandError(
                            f"Invalid CSV data at line {reader.line_num}: {exc}"
                        ) from exc

                    opis_id = values.pop("opis_truckstop_id")
                    stations[opis_id] = values
                    row_count += 1
        except (OSError, UnicodeError, csv.Error) as exc:
            raise CommandError(f"Could not read CSV: {exc}") from exc

        created_count = 0
        updated_count = 0
        with transaction.atomic():
            for opis_id, defaults in stations.items():
                _, created = FuelStation.objects.update_or_create(
                    opis_truckstop_id=opis_id,
                    defaults=defaults,
                )
                if created:
                    created_count += 1
                else:
                    updated_count += 1

        self.stdout.write(self.style.SUCCESS(
            f"Read {row_count} rows: created {created_count}, updated {updated_count}. "
            f"Collapsed {row_count - len(stations)} repeated OPIS rows (last row wins)."
        ))
