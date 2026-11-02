from django.db import models


class FuelStation(models.Model):
    """A truck stop from the OPIS price list, geocoded offline (see scripts/geocode_stations.py)."""

    opis_id = models.IntegerField(primary_key=True)
    name = models.CharField(max_length=128)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=64)
    state = models.CharField(max_length=2)
    retail_price = models.DecimalField(max_digits=10, decimal_places=8, help_text="USD per gallon")
    lat = models.FloatField()
    lon = models.FloatField()
    geo_source = models.CharField(max_length=16, default="gazetteer")

    class Meta:
        indexes = [models.Index(fields=["state", "city"])]

    def __str__(self):
        return f"{self.name} ({self.city}, {self.state}) ${self.retail_price:.3f}"
