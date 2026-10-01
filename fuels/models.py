from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class FuelStation(models.Model):
    """A truckstop and its retail fuel price from the supplied CSV."""

    opis_truckstop_id = models.PositiveIntegerField(unique=True)
    truckstop_name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2)
    rack_id = models.PositiveIntegerField()
    retail_price = models.DecimalField(
        max_digits=12,
        decimal_places=8,
        validators=[MinValueValidator(Decimal('0.00000001'))],
    )
    latitude = models.DecimalField(
        max_digits=8,
        decimal_places=6,
        null=True,
        blank=True,
        validators=[
            MinValueValidator(Decimal('-90')),
            MaxValueValidator(Decimal('90')),
        ],
    )
    longitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
        validators=[
            MinValueValidator(Decimal('-180')),
            MaxValueValidator(Decimal('180')),
        ],
    )

    def __str__(self):
        return f'{self.truckstop_name} ({self.city}, {self.state})'
