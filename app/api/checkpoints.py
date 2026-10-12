
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
)

router = APIRouter(prefix="/api/v1/routes", tags=["Checkpoints"])

MAX_WEATHER_CHECKPOINTS = 200
WEATHER_CONCURRENCY = 2


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

        route_response = await get_alternative_routes(route_request)

        sampled_routes = []
        total_checkpoints = 0

        # Generate checkpoints using the selected forecast time.
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

        # Protect the weather provider from oversized requests.
        if total_checkpoints > MAX_WEATHER_CHECKPOINTS:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"The selected interval produces {total_checkpoints} "
                    f"checkpoints across all routes. The current limit is "
                    f"{MAX_WEATHER_CHECKPOINTS}. Choose a larger interval."
                ),
            )

        semaphore = asyncio.Semaphore(WEATHER_CONCURRENCY)

        async def fetch_weather(checkpoint):
            async with semaphore:
                return await get_checkpoint_weather(
                    checkpoint_id=checkpoint.checkpoint_id,
                    latitude=checkpoint.latitude,
                    longitude=checkpoint.longitude,
                    eta=checkpoint.eta,
                )

        enriched_routes = []
        recommendation_inputs = []

        # Retrieve weather and classify checkpoint risk.
        for route, checkpoints in sampled_routes:
            weather_results = await asyncio.gather(
                *(fetch_weather(checkpoint) for checkpoint in checkpoints)
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
                        precipitation_in_hr=weather.precipitation_in_hr,
                        snowfall_in_hr=weather.snowfall_in_hr,
                        load_weight_lb=request.load_weight_lb,
                    )
                )

                planned_checkpoints.append(
                    PlannedCheckpoint(
                        **checkpoint.model_dump(),
                        weather=weather,
                        risk=risk,
                    )
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

        # Rank routes using risk first, then travel duration.
        summaries = rank_routes(recommendation_inputs)

        summaries_by_id = {
            summary.route_id: summary
            for summary in summaries
        }

        planned_routes = []

        for item in enriched_routes:
            route = item["route"]
            summary: RouteRiskSummary = summaries_by_id[route.route_id]

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

        # No-travel routes are not recommended.
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
            checkpoint_interval_miles=request.checkpoint_interval_miles,
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
            detail=f"Unable to retrieve checkpoint weather: {exc}",
        ) from exc

    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from exc
