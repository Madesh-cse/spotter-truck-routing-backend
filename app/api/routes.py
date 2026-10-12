
from fastapi import APIRouter, HTTPException

from app.schemas.routes import RouteRequest, RouteResponse
from app.services.routing import (
    RoutingServiceError,
    get_alternative_routes,
)

router = APIRouter(prefix="/api/v1/routes", tags=["Routes"])


@router.post("/alternatives", response_model=RouteResponse)
async def route_alternatives(
    request: RouteRequest,
) -> RouteResponse:
    try:
        return await get_alternative_routes(request)
    except RoutingServiceError as exc:
        message = str(exc)

        if "missing" in message.lower():
            status_code = 503
        elif "rate limit" in message.lower():
            status_code = 429
        elif "invalid" in message.lower() or "denied" in message.lower():
            status_code = 502
        else:
            status_code = 502

        raise HTTPException(
            status_code=status_code,
            detail=message,
        ) from exc
