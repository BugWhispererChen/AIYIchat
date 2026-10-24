from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import Response

from app.admin.auth import require_admin
from app.db.repositories.media_repo import MediaRepository

media_router = APIRouter()

_media_repo = MediaRepository()


@media_router.get("/{media_id}")
async def serve_media(
    request: Request,
    media_id: str,
    admin_session: str = Cookie(default=""),
):
    """读取媒体文件。

    新媒体的 `public_id` 是 URL 中的能力令牌（32 位随机值），持有完整 URL 即可读取；
    旧媒体只有可枚举的 ObjectId，未迁移前仅允许后台管理员会话访问，避免继续扩大
    枚举面。响应按私有资源缓存，禁止共享缓存留存。
    """
    doc = await _media_repo.find_by_id(media_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="媒体文件不存在")

    if doc.get("public_id") != media_id:
        # 旧 ObjectId 路径：没有能力令牌，必须有管理员会话
        try:
            await require_admin(request, admin_session)
        except HTTPException:
            # 不区分"无权限"和"不存在"，避免确认旧媒体是否存在
            raise HTTPException(status_code=404, detail="媒体文件不存在")

    return Response(
        content=doc["data"],
        media_type=doc.get("content_type", "application/octet-stream"),
        headers={
            "Cache-Control": "private, max-age=86400",
            "X-Content-Type-Options": "nosniff",
        },
    )
