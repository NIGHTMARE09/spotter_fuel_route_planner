from django.urls import path, re_path

from routing.views import RoutePlanView, route_map

urlpatterns = [
    re_path(r"^api/route/?$", RoutePlanView.as_view(), name="route-plan"),  # POST without the slash too
    path("map/", route_map, name="route-map"),
    path("", route_map),
]
