"""对话服务的数据库访问层。"""

from datetime import datetime, timezone
from typing import Optional

from app.db.mongo import get_database
from app.db.repositories.bot_config_repo import BotConfigRepository
from app.models.service import ServiceCreate, ServiceInDB, ServiceUpdate


class ServiceRepository:
    """管理对话服务的增删改查。"""

    def _collection(self):
        """获取 MongoDB 集合。"""
        return get_database().services

    async def find_by_id(self, service_id: str) -> Optional[ServiceInDB]:
        """根据 ID 查询对话服务。"""
        doc = await self._collection().find_one({"id": service_id})
        if doc is None:
            return None
        return ServiceInDB.from_mongo(doc)

    async def find_by_identity_and_bot(
        self, identity_id: str, bot_id: str
    ) -> Optional[ServiceInDB]:
        """根据身份 ID 和 Bot ID 查询激活的对话服务。"""
        doc = await self._collection().find_one(
            {"identity_id": identity_id, "bot_id": bot_id, "is_active": True}
        )
        if doc is None:
            return None
        return ServiceInDB.from_mongo(doc)

    async def list_by_account(
        self, account_id: str
    ) -> list[ServiceInDB]:
        """查询账号下的所有对话服务。"""
        cursor = (
            self._collection()
            .find({"account_id": account_id})
            .sort("created_at", -1)
        )
        return [ServiceInDB.from_mongo(doc) async for doc in cursor]

    async def list_by_identity(self, identity_id: str) -> list[ServiceInDB]:
        """查询身份参与的所有对话服务。"""
        cursor = (
            self._collection()
            .find({"identity_id": identity_id})
            .sort("created_at", -1)
        )
        return [ServiceInDB.from_mongo(doc) async for doc in cursor]

    async def list_by_bot(self, bot_id: str) -> list[ServiceInDB]:
        """查询 Bot 参与的所有对话服务。"""
        cursor = (
            self._collection()
            .find({"bot_id": bot_id})
            .sort("created_at", -1)
        )
        return [ServiceInDB.from_mongo(doc) async for doc in cursor]

    async def create(self, service: ServiceCreate) -> ServiceInDB:
        """创建新对话服务。"""
        now = datetime.now(timezone.utc)
        doc = service.model_dump()
        if doc.get("single_service_bot_id") is None:
            bot = await BotConfigRepository().find_by_id(service.bot_id)
            if bot is not None and bot.platform != "web":
                doc["single_service_bot_id"] = bot.id
        doc["created_at"] = now
        result = await self._collection().insert_one(doc)
        doc["_id"] = result.inserted_id
        return ServiceInDB.from_mongo(doc)

    async def update(
        self, service_id: str, update: ServiceUpdate
    ) -> Optional[ServiceInDB]:
        """更新对话服务。"""
        update_data = update.model_dump(exclude_none=True)
        if not update_data:
            return await self.find_by_id(service_id)
        await self._collection().update_one(
            {"id": service_id}, {"$set": update_data}
        )
        return await self.find_by_id(service_id)

    async def delete(self, service_id: str) -> bool:
        """删除对话服务。"""
        result = await self._collection().delete_one({"id": service_id})
        return result.deleted_count > 0

    async def delete_by_account(self, account_id: str) -> int:
        """删除账号下的所有对话服务。"""
        result = await self._collection().delete_many(
            {"account_id": account_id}
        )
        return result.deleted_count

    async def count_by_identity(self, identity_id: str) -> int:
        """统计引用该身份的服务数量（用于删除/禁用前的引用检查）。"""
        return await self._collection().count_documents({"identity_id": identity_id})

    async def count(self) -> int:
        """统计全局对话服务总数。"""
        return await self._collection().count_documents({})

    async def count_by_bot(self, bot_id: str) -> int:
        """统计引用该 Bot 的服务数量（用于删除/禁用前的引用检查）。"""
        return await self._collection().count_documents({"bot_id": bot_id})

    async def find_active_by_bot(self, bot_id: str) -> Optional[ServiceInDB]:
        """根据 bot_id 查找唯一的活跃服务（1 Bot = 1 Service 约束下）。"""
        doc = await self._collection().find_one(
            {"bot_id": bot_id, "is_active": True}
        )
        if doc is None:
            return None
        return ServiceInDB.from_mongo(doc)

    async def count_by_profile(self, profile_id: str) -> int:
        """统计引用该角色的服务数量（用于删除/禁用前的引用检查）。"""
        return await self._collection().count_documents({"profile_id": profile_id})

    async def list_active_bot_ids(self) -> set[str]:
        """查询所有活跃服务引用的 Bot ID 集合（用于过滤需要启动的 Bot）。"""
        cursor = self._collection().find(
            {"is_active": True},
            {"bot_id": 1, "_id": 0},
        )
        return {doc["bot_id"] async for doc in cursor}

    async def find_disabled_by_identity_and_bot(
        self, identity_id: str, bot_id: str
    ) -> Optional[ServiceInDB]:
        """查找被禁用的服务（按身份+Bot），用于引擎返回禁用提示。"""
        doc = await self._collection().find_one(
            {"identity_id": identity_id, "bot_id": bot_id, "is_active": False}
        )
        if doc is None:
            return None
        return ServiceInDB.from_mongo(doc)

    async def list_all_active(self) -> list[ServiceInDB]:
        """查询所有活跃的对话服务（用于主动消息调度器扫描）。"""
        cursor = self._collection().find({"is_active": True})
        return [ServiceInDB.from_mongo(doc) async for doc in cursor]

    async def list_by_share_group(
        self, share_group_id: str
    ) -> list[ServiceInDB]:
        """查询共享组内的所有服务（用于共享校验与删除保护）。"""
        cursor = self._collection().find({"share_group_id": share_group_id})
        return [ServiceInDB.from_mongo(doc) async for doc in cursor]
