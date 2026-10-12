
import pytest #type: ignore

from app.schemas.risk import (
    CheckpointRiskInput,
    RouteRecommendationInput,
    RouteSegmentRisk,
)
from app.services.risk import (
    apply_load_rules,
    classify_checkpoint,
    classify_rain,
    classify_snow,
    classify_wind,
    rank_routes,
)


@pytest.mark.parametrize(
    ("wind", "expected"),
    [
        (24.9, "low"),
        (25, "moderate"),
        (35, "high"),
        (45, "severe"),
        (55, "no_travel"),
    ],
)
def test_wind_thresholds(wind, expected):
    assert classify_wind(wind) == expected


@pytest.mark.parametrize(
    ("rain", "expected"),
    [
        (0.099, "low"),
        (0.10, "moderate"),
        (0.25, "high"),
        (0.50, "severe"),
        (1.00, "severe"),
        (1.001, "no_travel"),
    ],
)
def test_rain_thresholds(rain, expected):
    assert classify_rain(rain) == expected


@pytest.mark.parametrize(
    ("snow", "expected"),
    [
        (0.49, "low"),
        (0.5, "moderate"),
        (1.0, "high"),
        (2.0, "severe"),
        (3.0, "severe"),
        (3.01, "no_travel"),
    ],
)
def test_snow_thresholds(snow, expected):
    assert classify_snow(snow) == expected


def test_heavy_load_in_moderate_wind():
    assert apply_load_rules(40, 40_001, "high") == "severe"


def test_load_over_30000_in_high_wind():
    assert apply_load_rules(45, 30_001, "severe") == "no_travel"


def test_exact_load_threshold_does_not_trigger_override():
    assert apply_load_rules(45, 30_000, "severe") == "severe"


def test_worst_checkpoint_condition_wins():
    result = classify_checkpoint(
        CheckpointRiskInput(
            checkpoint_id="cp-1",
            wind_mph=20,
            precipitation_in_hr=0.6,
            snowfall_in_hr=0,
            load_weight_lb=20_000,
        )
    )

    assert result.wind_risk == "low"
    assert result.rain_risk == "severe"
    assert result.overall_risk == "severe"


def test_no_travel_route_ranks_last():
    routes = [
        RouteRecommendationInput(
            route_id="unsafe",
            duration_minutes=100,
            segments=[
                RouteSegmentRisk(
                    segment_miles=10, risk="no_travel"
                )
            ],
        ),
        RouteRecommendationInput(
            route_id="safe",
            duration_minutes=120,
            segments=[
                RouteSegmentRisk(
                    segment_miles=10, risk="moderate"
                )
            ],
        ),
    ]

    ranked = rank_routes(routes)

    assert ranked[0].route_id == "safe"
    assert ranked[0].recommended is True
    assert ranked[1].route_id == "unsafe"
    assert ranked[1].recommended is False


def test_severe_miles_take_priority_over_high_miles():
    routes = [
        RouteRecommendationInput(
            route_id="more-severe",
            duration_minutes=60,
            segments=[
                RouteSegmentRisk(segment_miles=5, risk="severe"),
            ],
        ),
        RouteRecommendationInput(
            route_id="less-severe",
            duration_minutes=300,
            segments=[
                RouteSegmentRisk(segment_miles=2, risk="severe"),
                RouteSegmentRisk(segment_miles=20, risk="high"),
            ],
        ),
    ]

    ranked = rank_routes(routes)

    assert ranked[0].route_id == "less-severe"
    assert ranked[0].severe_miles == 2
    assert ranked[1].severe_miles == 5
