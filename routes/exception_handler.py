"""DRF exception handler that renders every error as ``{"error": ...}``.

Handles both DRF's own errors (serializer validation, JSON parse errors,
404s on unmatched URLs, ...) and the domain exceptions defined in
``routes.exceptions``. Stack traces are never exposed to the client.
"""

from __future__ import annotations

from rest_framework.views import exception_handler as drf_exception_handler

from routes.exceptions import FuelRouteError


def _flatten_detail(detail) -> str:
    """Turn DRF error payloads (dict/list of strings) into a readable string."""
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict):
        parts = []
        for key, value in detail.items():
            parts.append(f"{key}: {_flatten_detail(value)}" if key != "detail" else _flatten_detail(value))
        return "; ".join(parts)
    if isinstance(detail, (list, tuple)):
        return "; ".join(_flatten_detail(item) for item in detail)
    return str(detail)


def custom_exception_handler(exc, context):
    # Domain errors raised by the service layer.
    if isinstance(exc, FuelRouteError):
        from rest_framework.response import Response

        return Response({"error": exc.message}, status=exc.status_code)

    response = drf_exception_handler(exc, context)
    if response is None:
        # Truly unexpected errors propagate to Django's 500 handling;
        # the view layer also catches these and returns a safe JSON body.
        return None

    response.data = {"error": _flatten_detail(response.data)}
    return response
