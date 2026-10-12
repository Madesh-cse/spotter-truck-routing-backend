
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.routes import Coordinates
from app.schemas.risk import CheckpointRiskResult, RouteRiskSummary
from app.schemas.weather import CheckpointWeather


class CheckpointRequest(BaseModel):
    origin: Coordinates
    destination: Coordinates
    departure_time: datetime
    forecast_offset_hours: int = Field(default=0, ge=0, le=48)
    checkpoint_interval_miles: Literal[10, 25, 50] = 25
    load_weight_lb: float = Field(ge=0, le=100_000)


class Checkpoint(BaseModel):
    checkpoint_id: str
    latitude: float
    longitude: float
    distance_from_start_miles: float
    segment_miles: float
    eta: datetime


class PlannedCheckpoint(Checkpoint):
    weather: CheckpointWeather
    risk: CheckpointRiskResult


class PlannedRoute(BaseModel):
    route_id: str
    distance_miles: float
    duration_minutes: float
    geometry: list[list[float]]
    checkpoints: list[PlannedCheckpoint]
    risk_summary: RouteRiskSummary


class CheckpointPlanResponse(BaseModel):
    departure_time: datetime
    forecast_offset_hours: int = 0
    checkpoint_interval_miles: int
    load_weight_lb: float
    routes: list[PlannedRoute]
    count: int
    recommended_route_id: str | None = None
    warning: str | None = None