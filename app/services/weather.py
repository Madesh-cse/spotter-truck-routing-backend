
import asyncio
import time
from datetime import datetime, timedelta, timezone

import httpx

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Cache hourly forecast responses for 15 minutes.
CACHE_TTL_SECONDS = 900
_weather_cache: dict[tuple[float, float], tuple[float, dict]] = {}
_cache_lock = asyncio.Lock()


class WeatherServiceError(Exception):
    pass


def to_utc_hour(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Checkpoint ETA must include a timezone.")

    value = value.astimezone(timezone.utc)
    value = value.replace(second=0, microsecond=0)

    if value.minute >= 30:
        value = value.replace(minute=0) + timedelta(hours=1)
    else:
        value = value.replace(minute=0)

    return value



async def get_hourly_forecast(
    latitude: float,
    longitude: float,
) -> dict:
    key = (round(latitude, 3), round(longitude, 3))

    async with _cache_lock:
        cached = _weather_cache.get(key)
        if cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]

    params = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": "wind_speed_10m,precipitation,snowfall",
        "wind_speed_unit": "kmh",
        "forecast_days": 7,
        "timezone": "UTC",
    }

    retryable_statuses = {429, 500, 502, 503, 504}
    max_attempts = 3

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            for attempt in range(max_attempts):
                try:
                    response = await client.get(
                        OPEN_METEO_URL,
                        params=params,
                    )

                    if (
                        response.status_code in retryable_statuses
                        and attempt < max_attempts - 1
                    ):
                        await asyncio.sleep(0.75 * (2 ** attempt))
                        continue

                    response.raise_for_status()
                    data = response.json()
                    break

                except httpx.HTTPStatusError as exc:
                    status_code = exc.response.status_code

                    if (
                        status_code in retryable_statuses
                        and attempt < max_attempts - 1
                    ):
                        await asyncio.sleep(0.75 * (2 ** attempt))
                        continue

                    raise WeatherServiceError(
                        f"Open-Meteo returned HTTP {status_code} "
                        f"after {attempt + 1} attempt(s)."
                    ) from exc

                except httpx.RequestError as exc:
                    if attempt < max_attempts - 1:
                        await asyncio.sleep(0.75 * (2 ** attempt))
                        continue

                    raise WeatherServiceError(
                        "Unable to connect to Open-Meteo after "
                        f"{max_attempts} attempts."
                    ) from exc

            else:
                raise WeatherServiceError(
                    "Open-Meteo did not return a forecast."
                )

    except httpx.RequestError as exc:
        raise WeatherServiceError(
            "Unable to connect to Open-Meteo."
        ) from exc

    if "hourly" not in data:
        raise WeatherServiceError(
            "Open-Meteo returned no hourly forecast."
        )

    async with _cache_lock:
        _weather_cache[key] = (time.monotonic(), data)

    return data



async def get_checkpoint_weather(
    checkpoint_id: str,
    latitude: float,
    longitude: float,
    eta: datetime,
) -> dict:
    if eta.tzinfo is None:
        raise ValueError("Checkpoint ETA must include a timezone.")

    target_hour = to_utc_hour(eta)
    data = await get_hourly_forecast(latitude, longitude)

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])

    if not times:
        raise WeatherServiceError(
            "Open-Meteo returned an empty hourly forecast."
        )

    # Parse available forecast timestamps as UTC.
    available_hours = []
    for index, value in enumerate(times):
        try:
            forecast_hour = datetime.strptime(
                value, "%Y-%m-%dT%H:%M"
            ).replace(tzinfo=timezone.utc)
            available_hours.append((forecast_hour, index))
        except (TypeError, ValueError):
            continue

    if not available_hours:
        raise WeatherServiceError(
            "Open-Meteo returned no valid hourly timestamps."
        )

    # Reject ETAs outside the forecast range. Do not use stale
    # weather data for checkpoints beyond the available window.
    first_hour = available_hours[0][0]
    last_hour = available_hours[-1][0]

    if target_hour < first_hour or target_hour > last_hour:
        raise WeatherServiceError(
            f"Checkpoint ETA {target_hour:%Y-%m-%dT%H:%M} UTC "
            f"is outside the available forecast window "
            f"({first_hour:%Y-%m-%dT%H:%M} to "
            f"{last_hour:%Y-%m-%dT%H:%M} UTC). "
            "Choose a departure time within the forecast window."
        )

    # Select the nearest available forecast hour.
    forecast_hour, index = min(
        available_hours,
        key=lambda item: abs(
            (item[0] - target_hour).total_seconds()
        ),
    )

    # Guard against unexpected gaps in hourly data.
    difference_minutes = abs(
        (forecast_hour - target_hour).total_seconds()
    ) / 60

    if difference_minutes > 60:
        raise WeatherServiceError(
            f"No suitable hourly forecast exists near "
            f"{target_hour:%Y-%m-%dT%H:%M} UTC."
        )

    def number_at(field: str) -> float:
        values = hourly.get(field, [])

        if index >= len(values) or values[index] is None:
            raise WeatherServiceError(
                f"Weather field '{field}' is unavailable "
                f"for {forecast_hour:%Y-%m-%dT%H:%M} UTC."
            )

        return float(values[index])

    wind_kmh = number_at("wind_speed_10m")
    precipitation_mm = number_at("precipitation")
    snowfall_cm = number_at("snowfall")

    return {
        "checkpoint_id": checkpoint_id,
        "forecast_time": forecast_hour,
        "eta": eta,
        "wind_mph": round(wind_kmh / 1.609344, 2),
        "precipitation_in_hr": round(
            precipitation_mm / 25.4, 4
        ),
        "snowfall_in_hr": round(snowfall_cm / 2.54, 4),
    }
