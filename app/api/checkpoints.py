
import asyncio
from datetime import timedelta

from fastapi import APIRouter, HTTPException

from app.schemas.checkpoints import (
    CheckpointPlanResponse,
    CheckpointRequest,
    PlannedCheckpoint,
    PlannedRoute,
)
from app.schemas.risk import (
    CheckpointRiskInput,
    RouteRecommendationInput,
    RouteRiskSummary,
    RouteSegmentRisk,
)
from app.schemas.routes import RouteRequest
from app.schemas.weather import CheckpointWeather
from app.services.checkpoints import sample_checkpoints
from app.services.risk import classify_checkpoint, rank_routes
from app.services.routing import (
    RoutingServiceError,
    get_alternative_routes,
)
from app.services.weather import (
    WeatherServiceError,
    get_checkpoint_weather,
    get_hourly_forecasts_batch,
)

router = APIRouter(
    prefix="/api/v1/routes",
    tags=["Checkpoints"],
)

MAX_WEATHER_CHECKPOINTS = 200


@router.post("/plan", response_model=CheckpointPlanResponse)
async def create_route_plan(
    request: CheckpointRequest,
) -> CheckpointPlanResponse:
    if request.departure_time.tzinfo is None:
        raise HTTPException(
            status_code=422,
            detail="departure_time must include a timezone.",
        )

    effective_departure_time = (
        request.departure_time
        + timedelta(hours=request.forecast_offset_hours)
    )

    try:
        route_request = RouteRequest(
            origin=request.origin,
            destination=request.destination,
        )

        route_response = await get_alternative_routes(
            route_request
        )

        sampled_routes = []
        total_checkpoints = 0

        # Generate checkpoints for every alternative route.
        for route in route_response.routes:
            checkpoints = sample_checkpoints(
                geometry=route.geometry,
                route_distance_miles=route.distance_miles,
                duration_minutes=route.duration_minutes,
                departure_time=effective_departure_time,
                interval_miles=request.checkpoint_interval_miles,
            )

            total_checkpoints += len(checkpoints)
            sampled_routes.append((route, checkpoints))

        # Protect the weather provider from oversized plans.
        if total_checkpoints > MAX_WEATHER_CHECKPOINTS:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"The selected interval produces "
                    f"{total_checkpoints} checkpoints across "
                    f"all routes. The current limit is "
                    f"{MAX_WEATHER_CHECKPOINTS}. "
                    "Choose a larger interval."
                ),
            )

        # Collect coordinates from all routes.
        all_checkpoints = [
            checkpoint
            for _, checkpoints in sampled_routes
            for checkpoint in checkpoints
        ]

        # Fetch and cache forecasts in batches.
        forecasts = await get_hourly_forecasts_batch([
            (
                checkpoint.latitude,
                checkpoint.longitude,
            )
            for checkpoint in all_checkpoints
        ])

        async def fetch_weather(checkpoint):
            key = (
                round(checkpoint.latitude, 3),
                round(checkpoint.longitude, 3),
            )

            forecast_data = forecasts.get(key)

            if forecast_data is None:
                raise WeatherServiceError(
                    "No forecast was returned for checkpoint "
                    f"{checkpoint.checkpoint_id}."
                )

            return await get_checkpoint_weather(
                checkpoint_id=checkpoint.checkpoint_id,
                latitude=checkpoint.latitude,
                longitude=checkpoint.longitude,
                eta=checkpoint.eta,
                forecast_data=forecast_data,
            )

        enriched_routes = []
        recommendation_inputs = []

        # Process each route using the fetched forecasts.
        for route, checkpoints in sampled_routes:
            weather_results = await asyncio.gather(
                *(
                    fetch_weather(checkpoint)
                    for checkpoint in checkpoints
                )
            )

            planned_checkpoints = []

            for checkpoint, weather_data in zip(
                checkpoints,
                weather_results,
                strict=True,
            ):
                weather = CheckpointWeather(**weather_data)

                risk = classify_checkpoint(
                    CheckpointRiskInput(
                        checkpoint_id=checkpoint.checkpoint_id,
                        wind_mph=weather.wind_mph,
                        precipitation_in_hr=(
                            weather.precipitation_in_hr
                        ),
                        snowfall_in_hr=weather.snowfall_in_hr,
                        load_weight_lb=request.load_weight_lb,
                    )
                )

                planned_checkpoint = PlannedCheckpoint(
                    **checkpoint.model_dump(),
                    weather=weather,
                    risk=risk,
                )

                planned_checkpoints.append(
                    planned_checkpoint
                )

            recommendation_inputs.append(
                RouteRecommendationInput(
                    route_id=route.route_id,
                    duration_minutes=route.duration_minutes,
                    segments=[
                        RouteSegmentRisk(
                            segment_miles=checkpoint.segment_miles,
                            risk=checkpoint.risk.overall_risk,
                        )
                        for checkpoint in planned_checkpoints
                    ],
                )
            )

            enriched_routes.append(
                {
                    "route": route,
                    "checkpoints": planned_checkpoints,
                }
            )

        # Rank routes by risk and travel duration.
        summaries = rank_routes(recommendation_inputs)

        summaries_by_id = {
            summary.route_id: summary
            for summary in summaries
        }

        planned_routes = []

        for item in enriched_routes:
            route = item["route"]

            summary: RouteRiskSummary = summaries_by_id[
                route.route_id
            ]

            planned_routes.append(
                PlannedRoute(
                    route_id=route.route_id,
                    distance_miles=route.distance_miles,
                    duration_minutes=route.duration_minutes,
                    geometry=route.geometry,
                    checkpoints=item["checkpoints"],
                    risk_summary=summary,
                )
            )

        planned_routes.sort(
            key=lambda route: route.risk_summary.rank
        )

        # Do not recommend a route marked unsafe for travel.
        recommended_route_id = next(
            (
                route.route_id
                for route in planned_routes
                if route.risk_summary.recommended
            ),
            None,
        )

        return CheckpointPlanResponse(
            departure_time=effective_departure_time,
            forecast_offset_hours=request.forecast_offset_hours,
            checkpoint_interval_miles=(
                request.checkpoint_interval_miles
            ),
            load_weight_lb=request.load_weight_lb,
            routes=planned_routes,
            count=len(planned_routes),
            recommended_route_id=recommended_route_id,
            warning=route_response.warning,
        )

    except HTTPException:
        raise

    except RoutingServiceError as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc

    except WeatherServiceError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "Unable to retrieve checkpoint weather: "
                f"{exc}"
            ),
        ) from exc

    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from exc
