from django.conf import settings
from rest_framework import serializers


class RouteQuerySerializer(serializers.Serializer):
    start = serializers.CharField(max_length=200, help_text="'City, ST', an address, or 'lat,lon'")
    finish = serializers.CharField(max_length=200, help_text="'City, ST', an address, or 'lat,lon'")
    start_fuel_gallons = serializers.FloatField(
        required=False,
        min_value=0,
        help_text="Fuel in the tank at departure. Defaults to a full tank.",
    )
    corridor_miles = serializers.FloatField(
        required=False,
        min_value=1,
        max_value=50,
        help_text="How far off the route a station may be.",
    )
    stop_cost = serializers.FloatField(
        required=False,
        min_value=0,
        max_value=100,
        help_text="Dollars a stop is worth avoiding (time, detour). 0 = cheapest fuel regardless of stops.",
    )
    reserve_miles = serializers.FloatField(
        required=False,
        min_value=0,
        max_value=250,
        help_text="Fuel (in miles of driving) the plan never dips into.",
    )
    full_geometry = serializers.BooleanField(required=False, default=False)

    def validate_start_fuel_gallons(self, value):
        tank = settings.VEHICLE_MAX_RANGE_MILES / settings.VEHICLE_MPG
        if value > tank:
            raise serializers.ValidationError(f"Tank holds at most {tank:g} gallons.")
        return value

    def validate(self, attrs):
        attrs.setdefault("start_fuel_gallons", settings.VEHICLE_MAX_RANGE_MILES / settings.VEHICLE_MPG)
        attrs.setdefault("corridor_miles", settings.DEFAULT_CORRIDOR_MILES)
        attrs.setdefault("stop_cost", settings.DEFAULT_STOP_COST)
        attrs.setdefault("reserve_miles", settings.DEFAULT_RESERVE_MILES)
        if attrs["start_fuel_gallons"] * settings.VEHICLE_MPG < attrs["reserve_miles"]:
            raise serializers.ValidationError(
                {"reserve_miles": "The starting fuel is already below the reserve; lower reserve_miles or add fuel."}
            )
        return attrs
