
from email.mime import message
import json

import httpx

from app.core.config import settings
from app.schemas.routes import (
    Coordinates,
    RouteRequest,
    RouteResponse,
    RouteSummary,
)

ORS_DIRECTIONS_URL = (
    "https://api.openrouteservice.org/v2/directions/"
    "driving-hgv/geojson"
)

METERS_PER_MILE = 1609.344


class RoutingServiceError(Exception):
    pass



async def get_alternative_routes(
    request: RouteRequest,
) -> RouteResponse:
    if not settings.ors_api_key:
        raise RoutingServiceError(
            "ORS_API_KEY is missing. Configure it in backend/.env."
        )

    coordinates = [
        [
            request.origin.longitude,
            request.origin.latitude,
        ],
        [
            request.destination.longitude,
            request.destination.latitude,
        ],
    ]

    headers = {
        "Authorization": settings.ors_api_key,
        "Content-Type": "application/json",
    }

    alternative_options = {
        "target_count": 3,
        "weight_factor": 1.6,
        "share_factor": 0.8,
    }

    base_payload = {"coordinates": coordinates}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                ORS_DIRECTIONS_URL,
                json={
                    **base_payload,
                    "alternative_routes": alternative_options,
                },
                headers=headers,
            )

            warning = None

            if response.status_code == 400:
                try:
                    error_data = response.json().get("error", {})
                except ValueError:
                    error_data = {}

                error_message = error_data.get("message", "")

                if "100000" in error_message or "alternative Routes" in error_message:
                    response = await client.post(
                        ORS_DIRECTIONS_URL,
                        json=base_payload,
                        headers=headers,
                    )
                    warning = (
                        "This trip exceeds the provider's 100 km "
                        "alternative-routing limit. A standard route "
                        "was requested; fewer than three routes may "
                        "be available."
                    )

            response.raise_for_status()
            data = response.json()

    except httpx.HTTPStatusError as exc:
        raise RoutingServiceError(
            f"ORS returned HTTP {exc.response.status_code}: "
            f"{exc.response.text}"
        ) from exc

    except httpx.RequestError as exc:
        raise RoutingServiceError(
            "Unable to connect to the routing provider."
        ) from exc

    routes = []

    for index, feature in enumerate(
        data.get("features", []), start=1
    ):
        properties = feature.get("properties", {})
        summary = properties.get("summary", {})
        geometry = feature.get("geometry", {})

        coords = geometry.get("coordinates", [])
        distance = summary.get("distance")
        duration = summary.get("duration")

        if not coords or distance is None or duration is None:
            continue

        routes.append(
            RouteSummary(
                route_id=f"route-{index}",
                distance_miles=round(distance / 1609.344, 2),
                duration_minutes=round(duration / 60, 2),
                geometry=coords,
            )
        )

    if not routes:
        raise RoutingServiceError(
            "ORS returned no usable routes."
        )

    if len(routes) < 3 and warning is None:
        warning = (
            f"ORS returned {len(routes)} route(s); "
            "three alternatives were requested."
        )

    return RouteResponse(
        routes=routes,
        count=len(routes),
        warning=warning,
    )
