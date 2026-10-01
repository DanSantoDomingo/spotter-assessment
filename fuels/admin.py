from django.contrib import admin

from fuels.models import FuelStation


@admin.register(FuelStation)
class FuelStationAdmin(admin.ModelAdmin):
    list_display = (
        'opis_truckstop_id',
        'truckstop_name',
        'city',
        'state',
        'retail_price',
        'latitude',
        'longitude',
    )
    list_display_links = ('opis_truckstop_id', 'truckstop_name')
    search_fields = ('=opis_truckstop_id', 'truckstop_name', 'address', 'city')
    list_filter = ('state',)
    ordering = ('opis_truckstop_id',)
    list_per_page = 50
