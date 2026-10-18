from fastapi import APIRouter

from app.api.auth_api import auth_router
from app.api.conversations_api import conversations_router
from app.api.health import health_router
from app.api.media_api import media_router
from app.api.proactive_api import proactive_router
from app.api.profiles_api import profiles_router
from app.api.web_profiles_api import web_profiles_router
from app.api.web_services_api import web_services_router

api_router = APIRouter()
api_router.include_router(health_router, tags=["health"])
api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(profiles_router, prefix="/profiles", tags=["profiles"])
api_router.include_router(conversations_router, prefix="/conversations", tags=["conversations"])
api_router.include_router(media_router, prefix="/media", tags=["media"])
api_router.include_router(proactive_router, prefix="/proactive", tags=["proactive"])
# C 端网页用户自助管理（角色 / 服务）：全部端点走 require_web_user 会话鉴权
api_router.include_router(web_profiles_router, prefix="/web/profiles", tags=["web-profiles"])
api_router.include_router(web_services_router, prefix="/web/services", tags=["web-services"])
