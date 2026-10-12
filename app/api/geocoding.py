from fastapi import APIRouter, HTTPException, Query
import logging
import httpx

from app.core.config import settings

router = APIRouter(prefix="/api/v1/geocoding", tags=["Geocoding"])

logger = logging.getLogger(__name__)


@router.get("/search")
async def search_locations(
    q: str = Query(..., min_length=2, max_length=150),
):
    api_key = settings.ors_api_key.strip()

    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="Geocoding service is not configured.",
        )

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                "https://api.openrouteservice.org/geocode/search",
                params={
                    "api_key": api_key,
                    "text": q,
                    "size": 5,
                },
            )

            response.raise_for_status()
            data = response.json()

    except httpx.HTTPStatusError as exc:
        # Do not log the request URL: it may contain the API key.
        logger.error(
            "ORS geocoding failed: status=%s, body=%s",
            exc.response.status_code,
            exc.response.text[:500],
        )

        raise HTTPException(
            status_code=502,
            detail="The geocoding provider returned an error. Check backend logs.",
        ) from exc

    except httpx.RequestError as exc:
        logger.error(
            "ORS geocoding connection failed: %s",
            type(exc).__name__,
        )

        raise HTTPException(
            status_code=502,
            detail="Unable to reach the geocoding provider.",
        ) from exc

    locations = []

    for feature in data.get("features", []):
        properties = feature.get("properties", {})
        coordinates = feature.get("geometry", {}).get("coordinates", [])

        if len(coordinates) < 2:
            continue

        locations.append(
            {
                "label": properties.get("label", q),
                "longitude": coordinates[0],
                "latitude": coordinates[1],
            }
        )

    return {"results": locations}