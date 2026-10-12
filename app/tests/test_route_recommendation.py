
from app.schemas.risk import RouteRecommendationInput, RouteSegmentRisk
from app.services.risk import rank_routes


def test_route_with_no_travel_is_never_recommended():
    routes = [
        RouteRecommendationInput(
            route_id="safe",
            duration_minutes=120,
            segments=[
                RouteSegmentRisk(
                    segment_miles=50,
                    risk="moderate",
                )
            ],
        ),
        RouteRecommendationInput(
            route_id="unsafe",
            duration_minutes=60,
            segments=[
                RouteSegmentRisk(
                    segment_miles=50,
                    risk="no_travel",
                )
            ],
        ),
    ]

    results = rank_routes(routes)
    by_id = {route.route_id: route for route in results}

    assert by_id["safe"].recommended is True
    assert by_id["unsafe"].recommended is False
    assert by_id["unsafe"].rank > by_id["safe"].rank


def test_severe_miles_take_priority_over_shorter_duration():
    routes = [
        RouteRecommendationInput(
            route_id="shorter",
            duration_minutes=60,
            segments=[
                RouteSegmentRisk(
                    segment_miles=20,
                    risk="severe",
                )
            ],
        ),
        RouteRecommendationInput(
            route_id="safer",
            duration_minutes=120,
            segments=[
                RouteSegmentRisk(
                    segment_miles=20,
                    risk="low",
                )
            ],
        ),
    ]

    results = rank_routes(routes)

    assert results[0].route_id == "safer"
    assert results[0].rank == 1
