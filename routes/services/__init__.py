"""Service layer for the routes app.

External integrations and business logic are isolated here so that views and
serializers stay thin:

    geocoding.py      Nominatim (OpenStreetMap) geocoding + DB cache
    routing.py        OSRM driving routes
    fuel_data.py      CSV fuel-price loading / validation / caching
    fuel_optimizer.py route-corridor matching + cost-optimal refueling
    geo_utils.py      haversine, route densification, point-to-route projection
"""
