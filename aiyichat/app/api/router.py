from fastapi import APIRouter

from app.api.conversations_api import conversations_router
from app.api.health import health_router
from app.api.media_api import media_router
from app.api.proactive_api import proactive_router
from app.api.profiles_api import profiles_router

api_router = APIRouter()
api_router.include_router(health_router, tags=["health"])
api_router.include_router(profiles_router, prefix="/profiles", tags=["profiles"])
api_router.include_router(conversations_router, prefix="/conversations", tags=["conversations"])
api_router.include_router(media_router, prefix="/media", tags=["media"])
api_router.include_router(proactive_router, prefix="/proactive", tags=["proactive"])
