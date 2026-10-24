import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from bson import ObjectId

from app.db.mongo import get_database

# 消息 media_url 形如 /api/media/{public_id 或旧 ObjectId}。新文档使用 32 位随机
# public_id 作为不可枚举的能力令牌；旧数据仍可能是 24 位 ObjectId（仅管理员可读）。
_MEDIA_URL_RE = re.compile(r"/api/media/([A-Za-z0-9_-]{20,})")


def media_ids_from_message_docs(messages: Sequence[Any]) -> list[str]:
    """从消息（ORM 对象或投影后的 dict）的 media_url 反解出媒体文档 ID。

    同时吃两种入参是因为账号级联删除走的是裸集合投影，而账号中心走的是
    `MessageRepository.list_by_conversation_ids` 返回的 `MessageInDB`。
    """
    ids: list[str] = []
    for item in messages:
        url = item.get("media_url") if isinstance(item, dict) else getattr(item, "media_url", "")
        ids.extend(_MEDIA_URL_RE.findall(url or ""))
    return ids


class MediaRepository:
    def _collection(self):
        return get_database().media

    async def create(
        self,
        data: bytes,
        content_type: str,
        filename: str = "",
    ) -> str:
        """保存媒体文件到 MongoDB，返回不可枚举的 public_id。

        public_id 是 URL 中的能力令牌：持有完整 URL 才能读取，替代可被推算的
        ObjectId；``_id`` 仍保留用于内部定位与旧数据兼容。
        """
        if not filename:
            ext = "png" if "png" in content_type else "gif" if "gif" in content_type else "jpg"
            filename = f"{uuid.uuid4().hex}.{ext}"

        public_id = uuid.uuid4().hex
        doc = {
            "public_id": public_id,
            "data": data,
            "content_type": content_type,
            "filename": filename,
            "size": len(data),
            "created_at": datetime.now(timezone.utc),
        }
        await self._collection().insert_one(doc)
        return public_id

    @staticmethod
    def _lookup_filter(media_id: str) -> Optional[dict]:
        """构造兼容新旧 ID 的查询条件；非法 ID 返回 None。"""
        clauses: list[dict] = [{"public_id": media_id}]
        if ObjectId.is_valid(media_id):
            clauses.append({"_id": ObjectId(media_id)})
        if not media_id:
            return None
        return {"$or": clauses}

    async def find_by_id(self, media_id: str) -> Optional[dict]:
        """按 public_id 或旧 ObjectId 读取媒体文件，返回含 data 字段的原始 dict。"""
        query = self._lookup_filter(media_id)
        if query is None:
            return None
        return await self._collection().find_one(query)

    async def delete(self, media_id: str) -> bool:
        query = self._lookup_filter(media_id)
        if query is None:
            return False
        result = await self._collection().delete_one(query)
        return result.deleted_count > 0

    async def delete_by_ids(self, media_ids: list[str]) -> int:
        """按 public_id 或旧 ObjectId 批量删除媒体文件。

        media 文档不记归属（只有二进制与上传时间），删除只能由持有消息的一方驱动：
        账号中心清空对话/注销时先从消息的 media_url 反解出 ID，再走这里清理，
        否则用户删完对话，生成的图片仍整份留在库里。
        """
        ids = [mid for mid in set(media_ids) if mid]
        if not ids:
            return 0
        object_ids = [ObjectId(mid) for mid in ids if ObjectId.is_valid(mid)]
        clauses: list[dict] = [{"public_id": {"$in": ids}}]
        if object_ids:
            clauses.append({"_id": {"$in": object_ids}})
        result = await self._collection().delete_many({"$or": clauses})
        return result.deleted_count
