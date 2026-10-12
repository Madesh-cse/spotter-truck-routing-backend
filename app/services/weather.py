
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx

logger = logging.getLogger(__name__)

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
                logger.warning(
                    "Could not parse Retry-After header: %r",
                    retry_after,
                )

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

    logger.info(
        "Starting Open-Meteo request: locations=%d, attempts=%d",
        len(coordinates),
        MAX_ATTEMPTS,
    )

    for attempt in range(MAX_ATTEMPTS):
        try:
            started_at = time.monotonic()

            response = await client.get(
                OPEN_METEO_URL,
                params=params,
            )

            elapsed = time.monotonic() - started_at

            logger.info(
                "Open-Meteo HTTP response: status=%d, "
                "locations=%d, attempt=%d/%d, elapsed=%.2fs",
                response.status_code,
                len(coordinates),
                attempt + 1,
                MAX_ATTEMPTS,
                elapsed,
            )

            if response.is_error:
                logger.error(
                    "Open-Meteo error response: status=%d, "
                    "body=%s",
                    response.status_code,
                    response.text[:2000],
                )

            if response.status_code in retryable_statuses:
                if attempt < MAX_ATTEMPTS - 1:
                    delay = _retry_delay(response, attempt)

                    logger.warning(
                        "Retrying Open-Meteo after HTTP %d "
                        "in %.2f seconds; attempt=%d/%d",
                        response.status_code,
                        delay,
                        attempt + 1,
                        MAX_ATTEMPTS,
                    )

                    await asyncio.sleep(delay)
                    continue

            response.raise_for_status()

            try:
                data = response.json()
            except ValueError as exc:
                logger.exception(
                    "Open-Meteo returned invalid JSON."
                )
                raise WeatherServiceError(
                    "Open-Meteo returned invalid JSON."
                ) from exc

            forecasts = data if isinstance(data, list) else [data]

            if len(forecasts) != len(coordinates):
                logger.error(
                    "Forecast count mismatch: expected=%d, received=%d",
                    len(coordinates),
                    len(forecasts),
                )

                raise WeatherServiceError(
                    "Open-Meteo returned an unexpected number "
                    "of forecasts for the requested coordinates."
                )

            for index, forecast in enumerate(forecasts):
                if (
                    not isinstance(forecast, dict)
                    or not isinstance(forecast.get("hourly"), dict)
                ):
                    logger.error(
                        "Invalid forecast structure at index=%d; "
                        "type=%s, keys=%s",
                        index,
                        type(forecast).__name__,
                        list(forecast.keys())
                        if isinstance(forecast, dict)
                        else None,
                    )

                    raise WeatherServiceError(
                        "Open-Meteo returned an invalid hourly forecast."
                    )

            logger.info(
                "Open-Meteo forecasts received successfully: count=%d",
                len(forecasts),
            )

            return forecasts

        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code

            logger.error(
                "Open-Meteo HTTP failure: status=%d, "
                "attempt=%d/%d, response_body=%s",
                status_code,
                attempt + 1,
                MAX_ATTEMPTS,
                exc.response.text[:2000],
            )

            if (
                status_code in retryable_statuses
                and attempt < MAX_ATTEMPTS - 1
            ):
                delay = _retry_delay(exc.response, attempt)

                logger.warning(
                    "Retrying after HTTP exception in %.2f seconds",
                    delay,
                )

                await asyncio.sleep(delay)
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
            logger.exception(
                "Open-Meteo connection/request failure: "
                "attempt=%d/%d, error_type=%s, error=%s",
                attempt + 1,
                MAX_ATTEMPTS,
                type(exc).__name__,
                str(exc),
            )

            if attempt < MAX_ATTEMPTS - 1:
                delay = min(2 ** attempt, 8)

                logger.warning(
                    "Retrying Open-Meteo connection in %.2f seconds",
                    delay,
                )

                await asyncio.sleep(delay)
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
    logger.info(
        "Fetching single-location forecast: latitude=%s, longitude=%s",
        key[0],
        key[1],
    )

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_SECONDS
    ) as client:
        forecasts = await _request_forecasts(client, [key])

    data = forecasts[0]

    async with _cache_lock:
        _weather_cache[key] = (time.monotonic(), data)

    logger.info(
        "Single-location forecast cached: coordinate=%s",
        key,
    )

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
                logger.info(
                    "Weather cache hit: coordinate=%s",
                    key,
                )
                return data

            logger.info(
                "Weather cache expired: coordinate=%s",
                key,
            )
            _weather_cache.pop(key, None)

        task = _inflight_requests.get(key)

        if task is None:
            logger.info(
                "Creating forecast request: coordinate=%s",
                key,
            )
            task = asyncio.create_task(_fetch_and_cache(key))
            _inflight_requests[key] = task
        else:
            logger.info(
                "Reusing in-flight forecast request: coordinate=%s",
                key,
            )

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
        logger.warning("Batch forecast requested with no coordinates.")
        return results

    logger.info(
        "Batch forecast requested: input_locations=%d, "
        "unique_locations=%d",
        len(coordinates),
        len(keys),
    )

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

        logger.info(
            "Batch forecast cache status: cached=%d, missing=%d",
            len(results),
            len(missing),
        )

        if not missing:
            return results

        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_SECONDS
        ) as client:
            for start in range(0, len(missing), BATCH_SIZE):
                batch = missing[start:start + BATCH_SIZE]

                logger.info(
                    "Processing forecast batch: batch_number=%d, "
                    "batch_size=%d",
                    start // BATCH_SIZE + 1,
                    len(batch),
                )

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

        logger.info(
            "Batch forecast completed: total_forecasts=%d",
            len(results),
        )

    return results


async def get_checkpoint_weather(
    checkpoint_id: str,
    latitude: float,
    longitude: float,
    eta: datetime,
    forecast_data: dict | None = None,
) -> dict:
    logger.info(
        "Building checkpoint weather: checkpoint_id=%s, "
        "latitude=%.4f, longitude=%.4f, eta=%s, provided_forecast=%s",
        checkpoint_id,
        latitude,
        longitude,
        eta.isoformat(),
        forecast_data is not None,
    )

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
        logger.error(
            "Empty hourly forecast: checkpoint_id=%s",
            checkpoint_id,
        )
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
            logger.debug(
                "Skipping invalid forecast timestamp: %r",
                value,
            )

    if not available_hours:
        logger.error(
            "No valid hourly timestamps: checkpoint_id=%s",
            checkpoint_id,
        )
        raise WeatherServiceError(
            "Open-Meteo returned no valid hourly timestamps."
        )

    first_hour = available_hours[0][0]
    last_hour = available_hours[-1][0]

    if target_hour < first_hour or target_hour > last_hour:
        logger.error(
            "Checkpoint ETA outside forecast window: checkpoint_id=%s, "
            "target=%s, first=%s, last=%s",
            checkpoint_id,
            target_hour.isoformat(),
            first_hour.isoformat(),
            last_hour.isoformat(),
        )
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
        logger.error(
            "No suitable forecast hour: checkpoint_id=%s, "
            "target=%s, nearest=%s, difference_minutes=%.1f",
            checkpoint_id,
            target_hour.isoformat(),
            forecast_hour.isoformat(),
            difference_minutes,
        )
        raise WeatherServiceError(
            f"No suitable hourly forecast exists near "
            f"{target_hour:%Y-%m-%dT%H:%M} UTC."
        )

    def number_at(field: str) -> float:
        values = hourly.get(field, [])

        if index >= len(values) or values[index] is None:
            logger.error(
                "Missing weather field: checkpoint_id=%s, "
                "field=%s, index=%d",
                checkpoint_id,
                field,
                index,
            )
            raise WeatherServiceError(
                f"Weather field '{field}' is unavailable "
                f"for {forecast_hour:%Y-%m-%dT%H:%M} UTC."
            )

        try:
            value = float(values[index])
        except (TypeError, ValueError) as exc:
            logger.exception(
                "Invalid weather field: checkpoint_id=%s, field=%s",
                checkpoint_id,
                field,
            )
            raise WeatherServiceError(
                f"Weather field '{field}' contains invalid data "
                f"for {forecast_hour:%Y-%m-%dT%H:%M} UTC."
            ) from exc

        if not (float("-inf") < value < float("inf")):
            logger.error(
                "Non-finite weather value: checkpoint_id=%s, "
                "field=%s, value=%r",
                checkpoint_id,
                field,
                value,
            )
            raise WeatherServiceError(
                f"Weather field '{field}' contains a non-finite "
                f"value for {forecast_hour:%Y-%m-%dT%H:%M} UTC."
            )

        return value

    wind_kmh = number_at("wind_speed_10m")
    precipitation_mm = number_at("precipitation")
    snowfall_cm = number_at("snowfall")

    result = {
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

    logger.info(
        "Checkpoint weather completed: checkpoint_id=%s, "
        "forecast_time=%s, wind_mph=%s, precipitation_in_hr=%s, "
        "snowfall_in_hr=%s",
        checkpoint_id,
        forecast_hour.isoformat(),
        result["wind_mph"],
        result["precipitation_in_hr"],
        result["snowfall_in_hr"],
    )

    return result
