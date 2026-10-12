"""Admin 配置热加载路由。"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.admin.auth import require_admin

settings_router = APIRouter(prefix="/admin/api", tags=["admin"])


@settings_router.post("/reload-settings")
async def reload_settings(_s: str = Depends(require_admin)) -> JSONResponse:
    """热重载配置（不重启服务），并重置 Provider / Vision / Embedding / Safety 缓存。"""
    from app.config import settings
    from app.conversation.safety_checker import reset_safety_checker
    from app.llm.provider_factory import reset_provider
    from app.services.embedding_service import reset_embedding_service
    from app.services.vision_service import reset_vision_service

    changed = await settings.reload()

    # 配置变更后统一重置缓存，各单例下次调用时惰性重建
    if changed:
        reset_provider()
        reset_vision_service()
        reset_embedding_service()
        reset_safety_checker()

    return JSONResponse({
        "status": "ok",
        "changed_fields": sorted(changed),
        "count": len(changed),
    })
