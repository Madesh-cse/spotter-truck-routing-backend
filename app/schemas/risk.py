
from typing import Literal

from pydantic import BaseModel, Field


RiskLevel = Literal[
    "low",
    "moderate",
    "high",
    "severe",
    "no_travel",
]


class CheckpointRiskInput(BaseModel):
    checkpoint_id: str
    wind_mph: float = Field(ge=0)
    precipitation_in_hr: float = Field(ge=0)
    snowfall_in_hr: float = Field(ge=0)
    load_weight_lb: float = Field(ge=0)


class CheckpointRiskResult(BaseModel):
    checkpoint_id: str
    wind_risk: RiskLevel
    rain_risk: RiskLevel
    snow_risk: RiskLevel
    overall_risk: RiskLevel
    load_weight_lb: float


class RouteRiskSummary(BaseModel):
    route_id: str
    has_no_travel: bool
    severe_miles: float
    high_miles: float
    average_risk: float
    duration_minutes: float
    recommended: bool
    rank: int


class RouteSegmentRisk(BaseModel):
    segment_miles: float = Field(ge=0)
    risk: RiskLevel


class RouteRecommendationInput(BaseModel):
    route_id: str
    duration_minutes: float = Field(ge=0)
    segments: list[RouteSegmentRisk]
