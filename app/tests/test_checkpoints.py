
from datetime import datetime, timedelta, timezone

import pytest  #type: ignore

from app.services.checkpoints import (
    haversine_miles,
    sample_checkpoints,
)


def test_haversine_distance():
    distance = haversine_miles(
        -122.4194, 37.7749,
        -122.4084, 37.7833,
    )

    assert 0.5 < distance < 1.5


def test_checkpoints_include_destination():
    departure = datetime(
        2026, 10, 10, 9, 0, tzinfo=timezone.utc
    )

    geometry = [
        [0.0, 0.0],
        [1.0, 0.0],
        [2.0, 0.0],
    ]

    checkpoints = sample_checkpoints(
        geometry=geometry,
        route_distance_miles=100,
        duration_minutes=120,
        departure_time=departure,
        interval_miles=25,
    )

    assert [
        cp.distance_from_start_miles for cp in checkpoints
    ] == [25, 50, 75, 100]

    assert checkpoints[-1].segment_miles == 25
    assert checkpoints[-1].longitude == pytest.approx(2.0)


def test_checkpoint_eta_uses_route_duration():
    departure = datetime(
        2026, 10, 10, 9, 0, tzinfo=timezone.utc
    )

    checkpoints = sample_checkpoints(
        geometry=[[0, 0], [1, 0]],
        route_distance_miles=100,
        duration_minutes=200,
        departure_time=departure,
        interval_miles=50,
    )

    assert checkpoints[0].eta == departure + timedelta(
        minutes=100
    )
    assert checkpoints[-1].eta == departure + timedelta(
        minutes=200
    )


def test_timezone_is_required():
    with pytest.raises(ValueError, match="timezone"):
        sample_checkpoints(
            geometry=[[0, 0], [1, 0]],
            route_distance_miles=100,
            duration_minutes=200,
            departure_time=datetime(2026, 10, 10, 9, 0),
            interval_miles=25,
        )


def test_invalid_interval_is_rejected():
    with pytest.raises(ValueError, match="Interval"):
        sample_checkpoints(
            geometry=[[0, 0], [1, 0]],
            route_distance_miles=100,
            duration_minutes=200,
            departure_time=datetime(
                2026, 10, 10, 9, 0, tzinfo=timezone.utc
            ),
            interval_miles=15,
        )
