
import asyncio
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

CACHE_TTL_SECONDS = 900
MAX_ATTEMPTS = 3
MAX_RETRY_DELAY_SECONDS = 30
REQUEST_TIMEOUT_SECONDS = 20.0
BATCH_SIZE = 10

_weather_cache: dict[
    tuple[float, float],
    tuple[float, dict],
] = {}

_inflight_requests: dict[
    tuple[float, float],
    asyncio.Task,
] = {}

_cache_lock = asyncio.Lock()
_batch_lock = asyncio.Lock()


class WeatherServiceError(Exception):
    pass


def _coordinate_key(
    latitude: float,
    longitude: float,
) -> tuple[float, float]:
    return round(latitude, 3), round(longitude, 3)


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


def _retry_delay(
    response: httpx.Response,
    attempt: int,
) -> float:
    retry_after = response.headers.get("Retry-After")

    if retry_after:
        try:
            return min(
                max(float(retry_after), 0.0),
                MAX_RETRY_DELAY_SECONDS,
            )
        except ValueError:
            try:
                retry_time = parsedate_to_datetime(retry_after)

                if retry_time.tzinfo is None:
                    retry_time = retry_time.replace(
                        tzinfo=timezone.utc
                    )

                delay = (
                    retry_time - datetime.now(timezone.utc)
                ).total_seconds()

                return min(
                    max(delay, 0.0),
                    MAX_RETRY_DELAY_SECONDS,
                )
            except (TypeError, ValueError, OverflowError):
                pass

    return min(2 ** attempt, MAX_RETRY_DELAY_SECONDS)


async def _request_forecasts(
    client: httpx.AsyncClient,
    coordinates: list[tuple[float, float]],
) -> list[dict]:
    params = {
        "latitude": ",".join(str(lat) for lat, _ in coordinates),
        "longitude": ",".join(str(lon) for _, lon in coordinates),
        "hourly": "wind_speed_10m,precipitation,snowfall",
        "wind_speed_unit": "kmh",
        "forecast_days": 7,
        "timezone": "UTC",
    }

    retryable_statuses = {429, 500, 502, 503, 504}

    for attempt in range(MAX_ATTEMPTS):
        try:
            response = await client.get(
                OPEN_METEO_URL,
                params=params,
            )

            if (
                response.status_code in retryable_statuses
                and attempt < MAX_ATTEMPTS - 1
            ):
                await asyncio.sleep(
                    _retry_delay(response, attempt)
                )
                continue

            response.raise_for_status()

            try:
                data = response.json()
            except ValueError as exc:
                raise WeatherServiceError(
                    "Open-Meteo returned invalid JSON."
                ) from exc

            forecasts = data if isinstance(data, list) else [data]

            if len(forecasts) != len(coordinates):
                raise WeatherServiceError(
                    "Open-Meteo returned an unexpected number "
                    "of forecasts for the requested coordinates."
                )

            for forecast in forecasts:
                if (
                    not isinstance(forecast, dict)
                    or not isinstance(forecast.get("hourly"), dict)
                ):
                    raise WeatherServiceError(
                        "Open-Meteo returned an invalid hourly forecast."
                    )

            return forecasts

        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code

            if (
                status_code in retryable_statuses
                and attempt < MAX_ATTEMPTS - 1
            ):
                await asyncio.sleep(
                    _retry_delay(exc.response, attempt)
                )
                continue

            if status_code == 429:
                raise WeatherServiceError(
                    "Open-Meteo rate limit reached after "
                    f"{attempt + 1} attempt(s)."
                ) from exc

            raise WeatherServiceError(
                f"Open-Meteo returned HTTP {status_code} "
                f"after {attempt + 1} attempt(s)."
            ) from exc

        except httpx.RequestError as exc:
            if attempt < MAX_ATTEMPTS - 1:
                await asyncio.sleep(min(2 ** attempt, 8))
                continue

            raise WeatherServiceError(
                "Unable to connect to Open-Meteo after "
                f"{MAX_ATTEMPTS} attempts."
            ) from exc

    raise WeatherServiceError(
        "Open-Meteo did not return a forecast."
    )


async def _fetch_and_cache(
    key: tuple[float, float],
) -> dict:
    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_SECONDS
    ) as client:
        forecasts = await _request_forecasts(client, [key])

    data = forecasts[0]

    async with _cache_lock:
        now = time.monotonic()
        _weather_cache[key] = (now, data)

    return data


async def get_hourly_forecast(
    latitude: float,
    longitude: float,
) -> dict:
    key = _coordinate_key(latitude, longitude)

    async with _cache_lock:
        cached = _weather_cache.get(key)

        if cached:
            created_at, data = cached

            if time.monotonic() - created_at < CACHE_TTL_SECONDS:
                return data

            _weather_cache.pop(key, None)

        task = _inflight_requests.get(key)

        if task is None:
            task = asyncio.create_task(_fetch_and_cache(key))
            _inflight_requests[key] = task

    try:
        return await asyncio.shield(task)
    finally:
        async with _cache_lock:
            if _inflight_requests.get(key) is task and task.done():
                _inflight_requests.pop(key, None)


async def get_hourly_forecasts_batch(
    coordinates: list[tuple[float, float]],
) -> dict[tuple[float, float], dict]:
    keys = list(
        dict.fromkeys(
            _coordinate_key(latitude, longitude)
            for latitude, longitude in coordinates
        )
    )

    results: dict[tuple[float, float], dict] = {}

    if not keys:
        return results

    # Serialize batch calls to avoid duplicate requests from this service.
    async with _batch_lock:
        now = time.monotonic()

        async with _cache_lock:
            expired_keys = [
                key
                for key, (created_at, _) in _weather_cache.items()
                if now - created_at >= CACHE_TTL_SECONDS
            ]

            for key in expired_keys:
                _weather_cache.pop(key, None)

            for key in keys:
                cached = _weather_cache.get(key)

                if cached:
                    results[key] = cached[1]

        missing = [key for key in keys if key not in results]

        if not missing:
            return results

        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_SECONDS
        ) as client:
            for start in range(0, len(missing), BATCH_SIZE):
                batch = missing[start:start + BATCH_SIZE]

                forecasts = await _request_forecasts(
                    client,
                    batch,
                )

                cached_at = time.monotonic()

                async with _cache_lock:
                    for key, forecast in zip(
                        batch,
                        forecasts,
                        strict=True,
                    ):
                        _weather_cache[key] = (
                            cached_at,
                            forecast,
                        )
                        results[key] = forecast

    return results


async def get_checkpoint_weather(
    checkpoint_id: str,
    latitude: float,
    longitude: float,
    eta: datetime,
    forecast_data: dict | None = None,
) -> dict:
    if eta.tzinfo is None:
        raise ValueError("Checkpoint ETA must include a timezone.")

    target_hour = to_utc_hour(eta)

    if forecast_data is None:
        data = await get_hourly_forecast(
            latitude=latitude,
            longitude=longitude,
        )
    else:
        data = forecast_data

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])

    if not times:
        raise WeatherServiceError(
            "Open-Meteo returned an empty hourly forecast."
        )

    available_hours = []

    for index, value in enumerate(times):
        try:
            forecast_hour = datetime.strptime(
                value,
                "%Y-%m-%dT%H:%M",
            ).replace(tzinfo=timezone.utc)

            available_hours.append((forecast_hour, index))

        except (TypeError, ValueError):
            continue

    if not available_hours:
        raise WeatherServiceError(
            "Open-Meteo returned no valid hourly timestamps."
        )

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

    forecast_hour, index = min(
        available_hours,
        key=lambda item: abs(
            (item[0] - target_hour).total_seconds()
        ),
    )

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

        try:
            value = float(values[index])
        except (TypeError, ValueError) as exc:
            raise WeatherServiceError(
                f"Weather field '{field}' contains invalid data "
                f"for {forecast_hour:%Y-%m-%dT%H:%M} UTC."
            ) from exc

        if not (float("-inf") < value < float("inf")):
            raise WeatherServiceError(
                f"Weather field '{field}' contains a non-finite "
                f"value for {forecast_hour:%Y-%m-%dT%H:%M} UTC."
            )

        return value

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
        "snowfall_in_hr": round(
            snowfall_cm / 2.54, 4
        ),
    }
