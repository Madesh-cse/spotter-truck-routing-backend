
from pydantic import BaseModel, Field


class Coordinates(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class RouteRequest(BaseModel):
    origin: Coordinates
    destination: Coordinates


class RouteSummary(BaseModel):
    route_id: str
    distance_miles: float
    duration_minutes: float
    geometry: list[list[float]]


class RouteResponse(BaseModel):
    routes: list[RouteSummary]
    count: int
    warning: str | None = None
