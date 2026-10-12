"""表情包集合仓储层（MongoDB stickers 集合）。"""

from typing import Optional

from bson import ObjectId

from app.db.mongo import get_database
from app.models.sticker import StickerDocument


class StickerRepository:
    """表情包集合的 CRUD 与检索查询。"""

    def _collection(self):
        return get_database().stickers

    async def create(self, sticker: StickerDocument) -> str:
        """插入一条表情包记录，返回文档 ID 字符串。"""
        doc = sticker.model_dump(by_alias=False, exclude={"id"})
        result = await self._collection().insert_one(doc)
        return str(result.inserted_id)

    async def list_all(self) -> list[StickerDocument]:
        """列出全部表情包（含停用，管理后台展示用）。"""
        docs = await self._collection().find(
            {}, sort=[("created_at", -1)]
        ).to_list(length=1000)
        return [s for d in docs if (s := StickerDocument.from_mongo(d))]

    async def list_enabled(self) -> list[StickerDocument]:
        """列出全部启用的表情包（检索服务与斗图选图用）。"""
        docs = await self._collection().find({"enabled": True}).to_list(length=1000)
        return [s for d in docs if (s := StickerDocument.from_mongo(d))]

    async def find_by_id(self, sticker_id: str) -> Optional[StickerDocument]:
        """按 ID 查找单条表情包。"""
        if not ObjectId.is_valid(sticker_id):
            return None
        doc = await self._collection().find_one({"_id": ObjectId(sticker_id)})
        return StickerDocument.from_mongo(doc)

    async def get_by_hash(self, content_hash: str) -> Optional[StickerDocument]:
        """按图片内容哈希查找（预置资源导入去重）。"""
        if not content_hash:
            return None
        doc = await self._collection().find_one({"content_hash": content_hash})
        return StickerDocument.from_mongo(doc)

    async def update_fields(self, sticker_id: str, fields: dict) -> bool:
        """按 ID 局部更新字段（启用开关、标签、描述等）。"""
        if not ObjectId.is_valid(sticker_id) or not fields:
            return False
        result = await self._collection().update_one(
            {"_id": ObjectId(sticker_id)}, {"$set": fields}
        )
        return result.modified_count > 0

    async def set_embedding(self, sticker_id: str, embedding: list[float]) -> bool:
        """回填语义向量。"""
        return await self.update_fields(sticker_id, {"embedding": embedding})

    async def incr_usage_count(self, sticker_id: str) -> None:
        """使用次数 +1（命中发送后调用，失败不影响主链路）。"""
        if not ObjectId.is_valid(sticker_id):
            return
        await self._collection().update_one(
            {"_id": ObjectId(sticker_id)}, {"$inc": {"usage_count": 1}}
        )

    async def clear_group(self, group_id: str) -> int:
        """将引用指定分组的表情包 group_id 置空（删除分组时级联调用）。"""
        if not ObjectId.is_valid(group_id):
            return 0
        result = await self._collection().update_many(
            {"group_id": group_id}, {"$set": {"group_id": ""}}
        )
        return result.modified_count

    async def delete(self, sticker_id: str) -> bool:
        """删除表情包记录（媒体文件由调用方决定是否级联删除）。"""
        if not ObjectId.is_valid(sticker_id):
            return False
        result = await self._collection().delete_one({"_id": ObjectId(sticker_id)})
        return result.deleted_count > 0
