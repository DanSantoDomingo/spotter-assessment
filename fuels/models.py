from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models


class FuelStation(models.Model):
    """A truckstop and its retail fuel price from the supplied CSV."""

    # OPIS IDs repeat in the source data
    opis_truckstop_id = models.PositiveIntegerField(db_index=True)
    truckstop_name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2)
    rack_id = models.PositiveIntegerField()
    retail_price = models.DecimalField(
        max_digits=12,
        decimal_places=8,
        validators=[MinValueValidator(Decimal("0.00000001"))],
    )

    def __str__(self):
        return f"{self.truckstop_name} ({self.city}, {self.state})"
