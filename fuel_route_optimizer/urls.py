"""Root URL configuration for the fuel route optimizer project."""

from django.urls import include, path

urlpatterns = [
    path("api/", include("routes.urls")),
]
