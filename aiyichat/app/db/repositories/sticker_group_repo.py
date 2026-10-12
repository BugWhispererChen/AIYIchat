"""表情包分组集合仓储层（MongoDB sticker_groups 集合）。"""

from typing import Optional

from bson import ObjectId

from app.db.mongo import get_database
from app.models.sticker_group import StickerGroupDocument


class StickerGroupRepository:
    """表情包分组集合的 CRUD 查询。"""

    def _collection(self):
        return get_database().sticker_groups

    async def create(self, group: StickerGroupDocument) -> str:
        """插入一条分组记录，返回文档 ID 字符串。"""
        doc = group.model_dump(by_alias=False, exclude={"id"})
        result = await self._collection().insert_one(doc)
        return str(result.inserted_id)

    async def list_all(self) -> list[StickerGroupDocument]:
        """列出全部分组（含停用，管理后台展示用）。"""
        docs = await self._collection().find(
            {}, sort=[("created_at", 1)]
        ).to_list(length=500)
        return [g for d in docs if (g := StickerGroupDocument.from_mongo(d))]

    async def find_by_id(self, group_id: str) -> Optional[StickerGroupDocument]:
        """按 ID 查找单条分组。"""
        if not ObjectId.is_valid(group_id):
            return None
        doc = await self._collection().find_one({"_id": ObjectId(group_id)})
        return StickerGroupDocument.from_mongo(doc)

    async def find_by_name(self, name: str) -> Optional[StickerGroupDocument]:
        """按名称查找单条分组（名称唯一，创建查重与 seed upsert 用）。"""
        if not name.strip():
            return None
        doc = await self._collection().find_one({"name": name.strip()})
        return StickerGroupDocument.from_mongo(doc)

    async def update_fields(self, group_id: str, fields: dict) -> bool:
        """按 ID 局部更新字段（名称、描述、启用开关等）。"""
        if not ObjectId.is_valid(group_id) or not fields:
            return False
        result = await self._collection().update_one(
            {"_id": ObjectId(group_id)}, {"$set": fields}
        )
        return result.modified_count > 0

    async def delete(self, group_id: str) -> bool:
        """删除分组记录（引用表情包的 group_id 由调用方级联置空）。"""
        if not ObjectId.is_valid(group_id):
            return False
        result = await self._collection().delete_one({"_id": ObjectId(group_id)})
        return result.deleted_count > 0
