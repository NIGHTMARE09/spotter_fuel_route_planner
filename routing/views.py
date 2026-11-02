from urllib.parse import urlencode

from django.shortcuts import render
from django.urls import reverse
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import RouteQuerySerializer
from .services.geocoding import GeocodingError, GeocodingUnavailable
from .services.optimizer import NoFeasiblePlan
from .services.osrm import NoRoute, RoutingError
from .services.planner import plan_trip
from .services.stations import StationsUnavailable

# Client mistakes are 4xx, upstream/setup failures 5xx. Order matters: subclasses first.
ERRORS = (
    (GeocodingUnavailable, "geocoding_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE),
    (GeocodingError, "geocoding_failed", status.HTTP_400_BAD_REQUEST),
    (NoRoute, "no_route", status.HTTP_422_UNPROCESSABLE_ENTITY),
    (NoFeasiblePlan, "no_feasible_plan", status.HTTP_422_UNPROCESSABLE_ENTITY),
    (RoutingError, "routing_failed", status.HTTP_502_BAD_GATEWAY),
    (StationsUnavailable, "stations_not_loaded", status.HTTP_503_SERVICE_UNAVAILABLE),
)


class RoutePlanView(APIView):
    """Plan a US road trip with the cheapest fuel stops.

    GET  /api/route/?start=Chicago, IL&finish=Los Angeles, CA
    POST /api/route/  {"start": "...", "finish": "..."}
    """

    def get(self, request):
        return self._plan(request, request.query_params)

    def post(self, request):
        return self._plan(request, request.data)

    def _plan(self, request, data):
        q = RouteQuerySerializer(data=data)
        q.is_valid(raise_exception=True)
        try:
            result = plan_trip(**q.validated_data)
        except tuple(e for e, _, _ in ERRORS) as exc:
            code, http = next((c, h) for e, c, h in ERRORS if isinstance(exc, e))
            return Response({"error": code, "detail": str(exc)}, status=http)

        # The map page re-runs this exact query, so it gets every planning parameter.
        params = {k: v for k, v in q.validated_data.items() if k != "full_geometry"}
        result = {**result, "map_url": request.build_absolute_uri(reverse("route-map") + "?" + urlencode(params))}
        return Response(result)


def route_map(request):
    """Leaflet page that calls the API above and draws the route + fuel stops."""
    return render(request, "routing/map.html")
