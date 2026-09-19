"""Database models for the routes app."""

from django.db import models


class GeocodingCache(models.Model):
    """Persistent cache of geocoding results.

    Caching keeps the number of outbound Nominatim requests minimal (the
    service asks for at most one request per distinct location string, ever).
    """

    query_key = models.CharField(max_length=64, unique=True, db_index=True)
    query = models.CharField(max_length=255)
    latitude = models.FloatField()
    longitude = models.FloatField()
    display_name = models.TextField(blank=True, default="")
    state = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.query} -> ({self.latitude}, {self.longitude})"

    class Meta:
        verbose_name = "geocoding cache entry"
        verbose_name_plural = "geocoding cache entries"
