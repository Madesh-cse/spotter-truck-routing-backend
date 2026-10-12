
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router as routes_router
from app.core.config import settings
from app.api.checkpoints import router as checkpoints_router
from app.api.weather import router as weather_router
from app.api.geocoding import router as geocoding_router
from app.api.ai_assistant import router as ai_assistant_router

app = FastAPI(
    title=settings.app_name,
    description="Weather-aware route planning for truck drivers.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
    "http://localhost:3000",
    "http://127.0.0.1:3000",
],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(routes_router)
app.include_router(checkpoints_router)
app.include_router(weather_router)
app.include_router(geocoding_router)
app.include_router(ai_assistant_router)


@app.get("/")
def root():
    return {
        "message": "Spotter Truck Routing API is running",
        "status": "healthy",
    }


@app.get("/health")
def health_check():
    return {"status": "ok"}
