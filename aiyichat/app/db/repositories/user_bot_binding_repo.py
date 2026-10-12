"""用户与 Bot 绑定关系的数据库访问层。"""

from typing import Optional

from bson import ObjectId

from app.db.mongo import get_database
from app.models.user_bot_binding import UserBotBindingCreate, UserBotBindingInDB


class UserBotBindingRepository:
    """管理用户与 Bot 的绑定关系。"""

    def _collection(self):
        """获取 MongoDB 集合。"""
        return get_database().user_bot_bindings

    async def find_by_user(
        self, user_id: str
    ) -> list[UserBotBindingInDB]:
        """查询指定用户绑定的所有 Bot。"""
        cursor = self._collection().find({"user_id": user_id})
        return [
            UserBotBindingInDB.from_mongo(doc) async for doc in cursor
        ]

    async def find_by_user_and_bot(
        self, user_id: str, bot_id: str
    ) -> Optional[UserBotBindingInDB]:
        """查询指定用户与指定 Bot 的绑定关系。"""
        doc = await self._collection().find_one(
            {"user_id": user_id, "bot_id": bot_id}
        )
        if doc is None:
            return None
        return UserBotBindingInDB.from_mongo(doc)

    async def create(
        self, binding: UserBotBindingCreate
    ) -> UserBotBindingInDB:
        """创建新的绑定关系。"""
        doc = binding.model_dump()
        result = await self._collection().insert_one(doc)
        doc["_id"] = result.inserted_id
        return UserBotBindingInDB.from_mongo(doc)

    async def delete(self, binding_id: str) -> bool:
        """删除指定绑定关系。"""
        result = await self._collection().delete_one(
            {"_id": ObjectId(binding_id)}
        )
        return result.deleted_count > 0

    async def delete_by_user(self, user_id: str) -> int:
        """删除指定用户的所有绑定关系。"""
        result = await self._collection().delete_many({"user_id": user_id})
        return result.deleted_count

    async def list_users_for_bot(
        self, bot_id: str
    ) -> list[UserBotBindingInDB]:
        """查询绑定到指定 Bot 的所有用户。"""
        cursor = self._collection().find({"bot_id": bot_id})
        return [
            UserBotBindingInDB.from_mongo(doc) async for doc in cursor
        ]
