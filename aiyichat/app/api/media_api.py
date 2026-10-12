from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.db.repositories.media_repo import MediaRepository

media_router = APIRouter()

_media_repo = MediaRepository()


@media_router.get("/{media_id}")
async def serve_media(media_id: str):
    """从 MongoDB 读取媒体文件并返回二进制流。"""
    doc = await _media_repo.find_by_id(media_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="媒体文件不存在")

    return Response(
        content=doc["data"],
        media_type=doc.get("content_type", "application/octet-stream"),
        headers={"Cache-Control": "public, max-age=86400"},
    )
