from fastapi import APIRouter

from app.api.auth_api import auth_router
from app.api.conversations_api import conversations_router
from app.api.health import health_router
from app.api.media_api import media_router
from app.api.proactive_api import proactive_router
from app.api.profiles_api import profiles_router
from app.api.web_profiles_api import web_profiles_router
from app.api.web_services_api import web_services_router
from app.api.web_bots_api import web_bots_router
from app.api.web_account_api import web_account_router

api_router = APIRouter()
api_router.include_router(health_router, tags=["health"])
api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(profiles_router, prefix="/profiles", tags=["profiles"])
api_router.include_router(conversations_router, prefix="/conversations", tags=["conversations"])
api_router.include_router(media_router, prefix="/media", tags=["media"])
api_router.include_router(proactive_router, prefix="/proactive", tags=["proactive"])
# C 端网页身份自助管理（角色 / 服务）：全部端点走 require_current_identity 会话鉴权
api_router.include_router(web_profiles_router, prefix="/web/profiles", tags=["web-profiles"])
api_router.include_router(web_services_router, prefix="/web/services", tags=["web-services"])
api_router.include_router(web_bots_router, prefix="/web/bots", tags=["web-bots"])
# C 端账号中心（资料 / 密码 / 设备 / 额度 / 数据与隐私 / 注销）：走 require_web_account，
# 不要求账号已有活跃服务
api_router.include_router(web_account_router, prefix="/web/account", tags=["web-account"])
