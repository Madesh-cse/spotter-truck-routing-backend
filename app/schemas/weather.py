
from datetime import datetime

from pydantic import BaseModel, Field


class WeatherRequestPoint(BaseModel):
    checkpoint_id: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    eta: datetime


class CheckpointWeather(BaseModel):
    checkpoint_id: str
    forecast_time: datetime
    eta: datetime
    wind_mph: float
    precipitation_in_hr: float
    snowfall_in_hr: float


class WeatherBatchRequest(BaseModel):
    checkpoints: list[WeatherRequestPoint]


class WeatherBatchResponse(BaseModel):
    results: list[CheckpointWeather]
    count: int
    warning: str | None = None
