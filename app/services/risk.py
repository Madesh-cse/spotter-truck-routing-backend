
from app.schemas.risk import(
    CheckpointRiskInput,
    CheckpointRiskResult,
    RiskLevel,
    RouteRecommendationInput,
    RouteRiskSummary,
)

RISK_SCORE: dict[str, int] = {
    "low": 0,
    "moderate": 1,
    "high": 2,
    "severe": 3,
    "no_travel": 4,
}

RISK_ORDER = [
    "low",
    "moderate",
    "high",
    "severe",
    "no_travel",
]


def classify_wind(wind_mph: float) -> RiskLevel:
    if wind_mph < 25:
        return "low"
    if wind_mph < 35:
        return "moderate"
    if wind_mph < 45:
        return "high"
    if wind_mph < 55:
        return "severe"
    return "no_travel"


def classify_rain(precipitation_in_hr: float) -> RiskLevel:
    if precipitation_in_hr < 0.10:
        return "low"
    if precipitation_in_hr < 0.25:
        return "moderate"
    if precipitation_in_hr < 0.50:
        return "high"
    if precipitation_in_hr <= 1.00:
        return "severe"
    return "no_travel"


def classify_snow(snowfall_in_hr: float) -> RiskLevel:
    if snowfall_in_hr < 0.5:
        return "low"
    if snowfall_in_hr < 1.0:
        return "moderate"
    if snowfall_in_hr < 2.0:
        return "high"
    if snowfall_in_hr <= 3.0:
        return "severe"
    return "no_travel"


def apply_load_rules(
    wind_mph: float,
    load_weight_lb: float,
    wind_risk: RiskLevel,
) -> RiskLevel:
    # Wind >= 55 mph means no travel for every load.
    if wind_mph >= 55:
        return "no_travel"

    # Heavy loads cannot travel in wind of 45-54 mph.
    if 45 <= wind_mph < 55 and load_weight_lb > 30_000:
        return "no_travel"

    # Wind of 35-44 mph with a load over 40,000 lb escalates to severe.
    if 35 <= wind_mph < 45 and load_weight_lb > 40_000:
        return "severe"

    return wind_risk


def worst_risk(*levels: RiskLevel) -> RiskLevel:
    return max(levels, key=lambda level: RISK_SCORE[level])


def classify_checkpoint(
    checkpoint: CheckpointRiskInput,
) -> CheckpointRiskResult:
    wind_risk = classify_wind(checkpoint.wind_mph)
    wind_risk = apply_load_rules(
        wind_mph=checkpoint.wind_mph,
        load_weight_lb=checkpoint.load_weight_lb,
        wind_risk=wind_risk,
    )

    rain_risk = classify_rain(checkpoint.precipitation_in_hr)
    snow_risk = classify_snow(checkpoint.snowfall_in_hr)

    overall = worst_risk(wind_risk, rain_risk, snow_risk)

    return CheckpointRiskResult(
        checkpoint_id=checkpoint.checkpoint_id,
        wind_risk=wind_risk,
        rain_risk=rain_risk,
        snow_risk=snow_risk,
        overall_risk=overall,
        load_weight_lb=checkpoint.load_weight_lb,
    )
    
def score_route(
    route: RouteRecommendationInput,
) -> RouteRiskSummary:
    severe_miles = sum(
        segment.segment_miles
        for segment in route.segments
        if segment.risk == "severe"
    )

    high_miles = sum(
        segment.segment_miles
        for segment in route.segments
        if segment.risk == "high"
    )

    total_miles = sum(
        segment.segment_miles for segment in route.segments
    )

    has_no_travel = any(
        segment.risk == "no_travel"
        for segment in route.segments
    )

    if total_miles > 0:
        average_risk = sum(
            segment.segment_miles * RISK_SCORE[segment.risk]
            for segment in route.segments
        ) / total_miles
    else:
        average_risk = 0.0

    return RouteRiskSummary(
        route_id=route.route_id,
        has_no_travel=has_no_travel,
        severe_miles=round(severe_miles, 2),
        high_miles=round(high_miles, 2),
        average_risk=round(average_risk, 3),
        duration_minutes=route.duration_minutes,
        recommended=not has_no_travel,
        rank=0,
    )


def rank_routes(
    routes: list[RouteRecommendationInput],
) -> list[RouteRiskSummary]:
    summaries = [score_route(route) for route in routes]

    # No Travel routes always rank below routes without such a segment.
    summaries.sort(
        key=lambda route: (
            route.has_no_travel,
            route.severe_miles,
            route.high_miles,
            route.average_risk,
            route.duration_minutes,
            route.route_id,
        )
    )

    for rank, summary in enumerate(summaries, start=1):
        summary.rank = rank

    return summaries
