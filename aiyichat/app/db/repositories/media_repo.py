import uuid
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId

from app.db.mongo import get_database


class MediaRepository:
    def _collection(self):
        return get_database().media

    async def create(
        self,
        data: bytes,
        content_type: str,
        filename: str = "",
    ) -> str:
        """保存媒体文件到 MongoDB，返回文档 ID 字符串。"""
        if not filename:
            ext = "png" if "png" in content_type else "gif" if "gif" in content_type else "jpg"
            filename = f"{uuid.uuid4().hex}.{ext}"

        doc = {
            "data": data,
            "content_type": content_type,
            "filename": filename,
            "size": len(data),
            "created_at": datetime.now(timezone.utc),
        }
        result = await self._collection().insert_one(doc)
        return str(result.inserted_id)

    async def find_by_id(self, media_id: str) -> Optional[dict]:
        """按 ID 读取媒体文件，返回含 data 字段的原始 dict。"""
        doc = await self._collection().find_one({"_id": ObjectId(media_id)})
        return doc

    async def delete(self, media_id: str) -> bool:
        result = await self._collection().delete_one({"_id": ObjectId(media_id)})
        return result.deleted_count > 0

    async def delete_by_ids(self, media_ids: list[str]) -> int:
        """按 ID 批量删除媒体文件。

        media 文档不记归属（只有二进制与上传时间），删除只能由持有消息的一方驱动：
        账号中心清空对话/注销时先从消息的 media_url 反解出 ID，再走这里清理，
        否则用户删完对话，生成的图片仍整份留在库里。
        """
        valid_ids = [mid for mid in set(media_ids) if ObjectId.is_valid(mid)]
        if not valid_ids:
            return 0
        result = await self._collection().delete_many(
            {"_id": {"$in": [ObjectId(mid) for mid in valid_ids]}}
        )
        return result.deleted_count
