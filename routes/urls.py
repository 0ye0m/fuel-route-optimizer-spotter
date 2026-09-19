"""URL configuration for the routes app."""

from django.urls import path

from routes.views import RoutePlanAPIView

urlpatterns = [
    path("route/", RoutePlanAPIView.as_view(), name="route-plan"),
]
