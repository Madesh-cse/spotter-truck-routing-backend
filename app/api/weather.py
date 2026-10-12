
import asyncio

from fastapi import APIRouter, HTTPException

from app.schemas.weather import (
    CheckpointWeather,
    WeatherBatchRequest,
    WeatherBatchResponse,
)
from app.services.weather import (
    WeatherServiceError,
    get_checkpoint_weather,
)

router = APIRouter(prefix="/api/v1/weather", tags=["Weather"])


@router.post("/checkpoints", response_model=WeatherBatchResponse)
async def weather_for_checkpoints(
    request: WeatherBatchRequest,
) -> WeatherBatchResponse:
    if not request.checkpoints:
        return WeatherBatchResponse(results=[], count=0)

    if len(request.checkpoints) > 200:
        raise HTTPException(
            status_code=422,
            detail="Maximum 200 checkpoints per request.",
        )

    semaphore = asyncio.Semaphore(10)

    async def fetch(point):
        async with semaphore:
            return await get_checkpoint_weather(
                checkpoint_id=point.checkpoint_id,
                latitude=point.latitude,
                longitude=point.longitude,
                eta=point.eta,
            )

    try:
        results = await asyncio.gather(
            *(fetch(point) for point in request.checkpoints)
        )
    except (WeatherServiceError, ValueError) as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc

    return WeatherBatchResponse(
        results=[
            CheckpointWeather(**result) for result in results
        ],
        count=len(results),
    )
    