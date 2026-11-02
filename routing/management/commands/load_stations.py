import csv
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from routing.models import FuelStation
from routing.services.stations import reset_station_index


class Command(BaseCommand):
    help = "Load geocoded fuel stations (data/stations_geocoded.csv) into the database. Idempotent."

    def add_arguments(self, parser):
        parser.add_argument("--file", default=str(Path(settings.DATA_DIR) / "stations_geocoded.csv"))

    def handle(self, *args, file, **options):
        with open(file, encoding="utf-8") as fh:
            objs = [
                FuelStation(
                    opis_id=int(r["opis_id"]),
                    name=r["name"],
                    address=r["address"],
                    city=r["city"],
                    state=r["state"],
                    retail_price=Decimal(r["price"]),
                    lat=float(r["lat"]),
                    lon=float(r["lon"]),
                    geo_source=r["geo_source"],
                )
                for r in csv.DictReader(fh)
            ]
        with transaction.atomic():
            FuelStation.objects.bulk_create(
                objs,
                batch_size=1000,
                update_conflicts=True,
                unique_fields=["opis_id"],
                update_fields=["name", "address", "city", "state", "retail_price", "lat", "lon", "geo_source"],
            )
        reset_station_index()
        self.stdout.write(self.style.SUCCESS(f"Loaded {len(objs)} fuel stations."))
