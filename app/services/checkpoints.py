
import math
from datetime import datetime, timedelta, timezone

from app.schemas.checkpoints import Checkpoint


EARTH_RADIUS_MILES = 3958.7613


def haversine_miles(
    lon1: float,
    lat1: float,
    lon2: float,
    lat2: float,
) -> float:
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)

    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1_rad)
        * math.cos(lat2_rad)
        * math.sin(delta_lon / 2) ** 2
    )

    return 2 * EARTH_RADIUS_MILES * math.asin(
        math.sqrt(min(1.0, a))
    )


def interpolate_point(
    start: list[float],
    end: list[float],
    fraction: float,
) -> tuple[float, float]:
    """
    Interpolate [longitude, latitude] coordinates.
    This is suitable for short segments, not geodesic navigation.
    """
    lon = start[0] + (end[0] - start[0]) * fraction
    lat = start[1] + (end[1] - start[1]) * fraction
    return lon, lat


def sample_checkpoints(
    geometry: list[list[float]],
    route_distance_miles: float,
    duration_minutes: float,
    departure_time: datetime,
    interval_miles: int,
) -> list[Checkpoint]:
    if interval_miles not in (10, 25, 50):
        raise ValueError("Interval must be 10, 25, or 50 miles.")

    if route_distance_miles <= 0 or duration_minutes < 0:
        raise ValueError("Route distance must be positive and duration non-negative.")

    if len(geometry) < 2:
        raise ValueError("Route geometry must contain at least two points.")

    if departure_time.tzinfo is None:
        raise ValueError("departure_time must include a timezone.")

    # Measure each geometry segment using haversine distance.
    segment_lengths = []
    geometric_distance = 0.0

    for start, end in zip(geometry, geometry[1:]):
        length = haversine_miles(
            start[0], start[1], end[0], end[1]
        )
        segment_lengths.append(length)
        geometric_distance += length

    if geometric_distance <= 0:
        raise ValueError("Route geometry has zero length.")

    # ORS summary distance is authoritative. Scale geometry distances
    # to align checkpoint distances with the provider's route distance.
    scale = route_distance_miles / geometric_distance
    scaled_lengths = [length * scale for length in segment_lengths]

    cumulative = [0.0]
    for length in scaled_lengths:
        cumulative.append(cumulative[-1] + length)

    targets = []
    distance = float(interval_miles)

    while distance < route_distance_miles:
        targets.append(distance)
        distance += interval_miles

    # Always include the destination, even if it falls off-interval.
    targets.append(route_distance_miles)

    checkpoints = []

    for index, target in enumerate(targets, start=1):
        segment_index = len(scaled_lengths) - 1

        for i, segment_end in enumerate(cumulative[1:]):
            if target <= segment_end:
                segment_index = i
                break

        segment_start_distance = cumulative[segment_index]
        segment_length = scaled_lengths[segment_index]

        if segment_length == 0:
            fraction = 0.0
        else:
            fraction = (
                target - segment_start_distance
            ) / segment_length

        fraction = max(0.0, min(1.0, fraction))

        lon, lat = interpolate_point(
            geometry[segment_index],
            geometry[segment_index + 1],
            fraction,
        )

        elapsed_minutes = (
            target / route_distance_miles
        ) * duration_minutes

        eta = departure_time + timedelta(
            minutes=elapsed_minutes
        )

        previous_target = (
            targets[index - 2] if index > 1 else 0.0
        )

        checkpoints.append(
            Checkpoint(
                checkpoint_id=f"cp-{index}",
                latitude=lat,
                longitude=lon,
                distance_from_start_miles=round(target, 2),
                segment_miles=round(target - previous_target, 2),
                eta=eta,
            )
        )

    return checkpoints
