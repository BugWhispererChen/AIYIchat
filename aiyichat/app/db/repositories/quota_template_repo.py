"""额度模板数据库访问层。"""

from datetime import datetime, timezone
from typing import Optional


from app.db.mongo import get_database
from app.db.repositories.object_id import to_object_id
from app.models.quota_template import (
    QuotaTemplateCreate,
    QuotaTemplateInDB,
    QuotaTemplateUpdate,
)


class QuotaTemplateRepository:
    """管理额度模板。"""

    def _collection(self):
        """获取 MongoDB 集合。"""
        return get_database().quota_templates

    async def list_all(self) -> list[QuotaTemplateInDB]:
        """查询所有额度模板。"""
        cursor = self._collection().find().sort("created_at", -1)
        return [
            QuotaTemplateInDB.from_mongo(doc) async for doc in cursor
        ]

    async def find_by_id(
        self, template_id: str
    ) -> Optional[QuotaTemplateInDB]:
        """根据 ID 查询额度模板。"""
        oid = to_object_id(template_id)
        if oid is None:
            return None
        doc = await self._collection().find_one({"_id": oid})
        if doc is None:
            return None
        return QuotaTemplateInDB.from_mongo(doc)

    async def create(
        self, template: QuotaTemplateCreate
    ) -> QuotaTemplateInDB:
        """创建新的额度模板。"""
        doc = template.model_dump()
        doc["created_at"] = datetime.now(timezone.utc)
        result = await self._collection().insert_one(doc)
        doc["_id"] = result.inserted_id
        return QuotaTemplateInDB.from_mongo(doc)

    async def update(
        self,
        template_id: str,
        update: QuotaTemplateUpdate,
    ) -> Optional[QuotaTemplateInDB]:
        """更新额度模板。"""
        update_data = update.model_dump(exclude_none=True)
        if not update_data:
            return await self.find_by_id(template_id)
        oid = to_object_id(template_id)
        if oid is None:
            return None
        await self._collection().update_one({"_id": oid}, {"$set": update_data})
        return await self.find_by_id(template_id)

    async def delete(self, template_id: str) -> bool:
        """删除额度模板。"""
        oid = to_object_id(template_id)
        if oid is None:
            return False
        result = await self._collection().delete_one({"_id": oid})
        return result.deleted_count > 0
