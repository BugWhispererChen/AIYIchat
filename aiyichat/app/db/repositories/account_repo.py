"""账号的数据库访问层。"""

from datetime import datetime, timezone
from typing import Optional

from app.db.mongo import get_database
from app.models.account import AccountCreate, AccountInDB, AccountUpdate


class AccountRepository:
    """管理账号的增删改查。"""

    def _collection(self):
        """获取 MongoDB 集合。"""
        return get_database().accounts

    async def find_by_id(self, account_id: str) -> Optional[AccountInDB]:
        """根据 ID 查询账号。"""
        doc = await self._collection().find_one({"id": account_id})
        if doc is None:
            return None
        return AccountInDB.from_mongo(doc)

    async def create(self, account: AccountCreate) -> AccountInDB:
        """创建新账号。"""
        now = datetime.now(timezone.utc)
        doc = account.model_dump()
        doc["created_at"] = now
        doc["updated_at"] = now
        result = await self._collection().insert_one(doc)
        doc["_id"] = result.inserted_id
        return AccountInDB.from_mongo(doc)

    async def update(
        self, account_id: str, update: AccountUpdate
    ) -> Optional[AccountInDB]:
        """更新账号信息。"""
        update_data = update.model_dump(exclude_none=True)
        if not update_data:
            return await self.find_by_id(account_id)
        update_data["updated_at"] = datetime.now(timezone.utc)
        await self._collection().update_one(
            {"id": account_id}, {"$set": update_data}
        )
        return await self.find_by_id(account_id)

    async def delete(self, account_id: str) -> bool:
        """删除账号。"""
        result = await self._collection().delete_one({"id": account_id})
        return result.deleted_count > 0

    async def delete_cascade(self, account_id: str) -> dict[str, int]:
        """级联删除账号及其关联数据（服务→对话→消息→用户→Bot→额度→角色→账号）。"""
        db = get_database()
        stats: dict[str, int] = {}

        # 1. 查找账号下所有服务，逐个级联删除对话和消息
        svc_cursor = db.services.find({"account_id": account_id}, {"id": 1})
        svc_ids = [doc["id"] async for doc in svc_cursor]
        msg_count = 0
        conv_count = 0
        for svc_id in svc_ids:
            # 获取该服务下所有对话 ID
            conv_cursor = db.conversations.find({"service_id": svc_id}, {"_id": 1})
            conv_ids = [str(doc["_id"]) async for doc in conv_cursor]
            # 批量删除消息
            if conv_ids:
                msg_count += (await db.messages.delete_many(
                    {"conversation_id": {"$in": conv_ids}}
                )).deleted_count
            # 删除对话
            conv_count += (await db.conversations.delete_many(
                {"service_id": svc_id}
            )).deleted_count
        stats["messages"] = msg_count
        stats["conversations"] = conv_count

        # 1b. 批量删除所有服务的长期记忆
        if svc_ids:
            stats["service_memories"] = (await db.service_memories.delete_many(
                {"service_id": {"$in": svc_ids}}
            )).deleted_count
        else:
            stats["service_memories"] = 0

        # 2. 删除服务
        stats["services"] = (await db.services.delete_many({"account_id": account_id})).deleted_count
        # 3. 删除用户
        stats["identities"] = (await db.identities.delete_many({"account_id": account_id})).deleted_count
        # 4. 删除 Bot
        stats["bots"] = (await db.bot_configs.delete_many({"account_id": account_id})).deleted_count
        # 5. 删除额度
        stats["quotas"] = (await db.account_quotas.delete_many({"account_id": account_id})).deleted_count
        # 6. 删除私有角色
        stats["private_profiles"] = (
            await db.profiles.delete_many({"visibility": "private", "account_id": account_id})
        ).deleted_count
        # 7. 删除账号本身
        stats["account"] = (await self._collection().delete_one({"id": account_id})).deleted_count

        return stats

    async def list_all(
        self, skip: int = 0, limit: int = 50
    ) -> list[AccountInDB]:
        """分页查询账号列表。"""
        cursor = (
            self._collection()
            .find()
            .sort("created_at", -1)
            .skip(skip)
            .limit(limit)
        )
        return [AccountInDB.from_mongo(doc) async for doc in cursor]

    async def search(self, q: str, limit: int = 50) -> list[AccountInDB]:
        """按名称或备注搜索账号。"""
        regex = {"$regex": q, "$options": "i"}
        cursor = (
            self._collection()
            .find({"$or": [{"name": regex}, {"remark": regex}]})
            .sort("created_at", -1)
            .limit(limit)
        )
        return [AccountInDB.from_mongo(doc) async for doc in cursor]

    async def count(self) -> int:
        """统计账号总数。"""
        return await self._collection().count_documents({})
