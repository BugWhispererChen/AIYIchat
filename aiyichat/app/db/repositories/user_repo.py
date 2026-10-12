"""用户的数据库访问层。"""

from datetime import datetime, timezone
from typing import Literal, Optional

from app.db.mongo import get_database
from app.models.user import UserCreate, UserInDB, UserUpdate


class UserRepository:
    """管理用户的增删改查。"""

    def _collection(self):
        """获取 MongoDB 集合。"""
        return get_database().users

    async def find_by_platform(
        self,
        platform: str,
        platform_id: str,
    ) -> Optional[UserInDB]:
        """根据平台和平台 ID 查询用户。"""
        doc = await self._collection().find_one({
            "platform": platform,
            "platform_id": platform_id,
        })
        if doc is None:
            return None
        return UserInDB.from_mongo(doc)

    async def find_by_id(self, user_id: str) -> Optional[UserInDB]:
        """根据 ID 查询用户。"""
        doc = await self._collection().find_one({"id": user_id})
        if doc is None:
            return None
        return UserInDB.from_mongo(doc)

    async def create(self, user: UserCreate) -> UserInDB:
        """创建新用户。"""
        now = datetime.now(timezone.utc)
        doc = user.model_dump()
        doc["created_at"] = now
        doc["last_active_at"] = now
        result = await self._collection().insert_one(doc)
        doc["_id"] = result.inserted_id
        return UserInDB.from_mongo(doc)

    async def find_or_create(
        self,
        platform: Literal["qq", "wechat", "web"],
        platform_id: str,
        account_id: str,
        nickname: str = "",
    ) -> UserInDB:
        """查找或创建用户。"""
        user = await self.find_by_platform(platform, platform_id)
        if user is not None:
            await self._collection().update_one(
                {"id": user.id},
                {"$set": {
                    "last_active_at": datetime.now(timezone.utc),
                }},
            )
            return user
        return await self.create(
            UserCreate(
                account_id=account_id,
                platform_id=platform_id,
                platform=platform,
                nickname=nickname,
            )
        )

    async def update(
        self, user_id: str, update: UserUpdate
    ) -> Optional[UserInDB]:
        """更新用户信息。"""
        update_data = update.model_dump(exclude_none=True)
        if not update_data:
            return await self.find_by_id(user_id)
        await self._collection().update_one(
            {"id": user_id}, {"$set": update_data}
        )
        return await self.find_by_id(user_id)

    async def delete(self, user_id: str) -> bool:
        """删除用户。"""
        result = await self._collection().delete_one({"id": user_id})
        return result.deleted_count > 0

    async def delete_by_account(self, account_id: str) -> int:
        """删除账号下的所有用户。"""
        result = await self._collection().delete_many({"account_id": account_id})
        return result.deleted_count

    async def list_by_account(
        self, account_id: str, skip: int = 0, limit: int = 50
    ) -> list[UserInDB]:
        """查询账号下的用户列表。"""
        cursor = (
            self._collection()
            .find({"account_id": account_id})
            .sort("last_active_at", -1)
            .skip(skip)
            .limit(limit)
        )
        return [UserInDB.from_mongo(doc) async for doc in cursor]

    async def list_all(
        self, skip: int = 0, limit: int = 50
    ) -> list[UserInDB]:
        """分页查询用户列表。"""
        cursor = (
            self._collection()
            .find()
            .sort("last_active_at", -1)
            .skip(skip)
            .limit(limit)
        )
        return [UserInDB.from_mongo(doc) async for doc in cursor]

    async def count(self) -> int:
        """统计用户总数。"""
        return await self._collection().count_documents({})

    async def search(self, q: str, limit: int = 50) -> list[UserInDB]:
        """按名称、昵称、平台ID或平台搜索用户。"""
        regex = {"$regex": q, "$options": "i"}
        cursor = (
            self._collection()
            .find({"$or": [{"name": regex}, {"nickname": regex}, {"platform_id": regex}, {"platform": regex}]})
            .sort("last_active_at", -1)
            .limit(limit)
        )
        return [UserInDB.from_mongo(doc) async for doc in cursor]

    async def count_by_account(self, account_id: str) -> int:
        """统计账号下用户数量。"""
        return await self._collection().count_documents({"account_id": account_id})

    async def set_blocked(
        self, user_id: str, blocked: bool, reason: str = ""
    ) -> bool:
        """设置用户禁用/启用状态。"""
        result = await self._collection().update_one(
            {"id": user_id},
            {"$set": {
                "is_blocked": blocked,
                "block_reason": reason if blocked else "",
            }},
        )
        return result.modified_count > 0

    async def update_last_active(self, user_id: str) -> bool:
        """更新用户最后活跃时间为当前 UTC 时间。

        专用于在每轮对话处理完成后刷新 `last_active_at`，
        因 `UserUpdate` 模型不包含该字段，故走独立通道。
        """
        result = await self._collection().update_one(
            {"id": user_id},
            {"$set": {"last_active_at": datetime.now(timezone.utc)}},
        )
        return result.modified_count > 0

    async def bind_platform_id(
        self,
        user_id: str,
        platform_id: str,
        nickname: str = "",
    ) -> None:
        """将真实平台 openid 绑定到管理员预创建的用户上。

        同时更新 last_active_at 和 nickname（如果提供）。
        """
        update_fields: dict = {
            "platform_id": platform_id,
            "last_active_at": datetime.now(timezone.utc),
        }
        if nickname:
            update_fields["nickname"] = nickname
        await self._collection().update_one(
            {"id": user_id},
            {"$set": update_fields},
        )

    async def bind_bot_openid(
        self, user_id: str, bot_id: str, platform_id: str
    ) -> bool:
        """记录用户在指定 Bot（appid）下的 openid 映射。

        QQ 的 openid 按 appid 生成，同一用户在不同 Bot 下并不相同；
        主动消息发送时需按 Bot 路由到对应 openid，否则会因目标无效被平台拒绝。
        采用 `$ne` 过滤实现幂等写入（值未变化时不产生写操作）。

        Returns:
            是否实际发生了写入
        """
        if not user_id or not bot_id or not platform_id:
            return False
        result = await self._collection().update_one(
            {"id": user_id, f"bot_openids.{bot_id}": {"$ne": platform_id}},
            {"$set": {f"bot_openids.{bot_id}": platform_id}},
        )
        return result.modified_count > 0
