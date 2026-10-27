"""身份的数据库访问层。"""

from datetime import datetime, timezone
from typing import Literal, Optional

from app.db.mongo import get_database
from app.models.identity import IdentityCreate, IdentityInDB, IdentityUpdate


class IdentityRepository:
    """管理身份的增删改查。"""

    def _collection(self):
        """获取 MongoDB 集合。"""
        return get_database().identities

    async def find_by_platform(
        self,
        platform: str,
        platform_id: str,
    ) -> Optional[IdentityInDB]:
        """根据平台和平台 ID 查询身份。"""
        doc = await self._collection().find_one({
            "platform": platform,
            "platform_id": platform_id,
        })
        if doc is None:
            return None
        return IdentityInDB.from_mongo(doc)

    async def find_by_id(self, identity_id: str) -> Optional[IdentityInDB]:
        """根据 ID 查询身份。"""
        doc = await self._collection().find_one({"id": identity_id})
        if doc is None:
            return None
        return IdentityInDB.from_mongo(doc)

    async def create(self, identity: IdentityCreate) -> IdentityInDB:
        """创建新身份。"""
        now = datetime.now(timezone.utc)
        doc = identity.model_dump()
        doc["created_at"] = now
        doc["last_active_at"] = now
        result = await self._collection().insert_one(doc)
        doc["_id"] = result.inserted_id
        return IdentityInDB.from_mongo(doc)

    async def find_or_create(
        self,
        platform: Literal["qq", "wechat"],
        platform_id: str,
        account_id: str,
        nickname: str = "",
    ) -> IdentityInDB:
        """查找或创建身份。"""
        identity = await self.find_by_platform(platform, platform_id)
        if identity is not None:
            await self._collection().update_one(
                {"id": identity.id},
                {"$set": {
                    "last_active_at": datetime.now(timezone.utc),
                }},
            )
            return identity
        return await self.create(
            IdentityCreate(
                account_id=account_id,
                platform_id=platform_id,
                platform=platform,
                nickname=nickname,
            )
        )

    async def update(
        self, identity_id: str, update: IdentityUpdate
    ) -> Optional[IdentityInDB]:
        """更新身份信息。"""
        update_data = update.model_dump(exclude_none=True)
        if not update_data:
            return await self.find_by_id(identity_id)
        await self._collection().update_one(
            {"id": identity_id}, {"$set": update_data}
        )
        return await self.find_by_id(identity_id)

    async def delete(self, identity_id: str) -> bool:
        """删除身份。"""
        result = await self._collection().delete_one({"id": identity_id})
        return result.deleted_count > 0

    async def delete_cascade(self, identity_id: str) -> dict[str, int]:
        """删除身份及其业务数据；网页登录凭据仅随账号删除。"""
        from app.db.repositories.conversation_repo import ConversationRepository

        db = get_database()
        stats: dict[str, int] = {}
        stats["proactive_records"] = (
            await db.proactive_records.delete_many({"identity_id": identity_id})
        ).deleted_count
        conv_ids = [
            str(doc["_id"])
            async for doc in db.conversations.find(
                {"identity_id": identity_id}, {"_id": 1}
            )
        ]
        stats.update(await ConversationRepository().delete_cascade_by_ids(conv_ids))
        stats["identity"] = 1 if await self.delete(identity_id) else 0
        return stats

    async def delete_by_account(self, account_id: str) -> int:
        """删除账号下的所有身份。"""
        result = await self._collection().delete_many({"account_id": account_id})
        return result.deleted_count

    async def list_by_account(
        self, account_id: str, skip: int = 0, limit: int = 50
    ) -> list[IdentityInDB]:
        """查询账号下的身份列表。"""
        cursor = (
            self._collection()
            .find({"account_id": account_id})
            .sort("last_active_at", -1)
            .skip(skip)
            .limit(limit)
        )
        return [IdentityInDB.from_mongo(doc) async for doc in cursor]

    async def list_all(
        self, skip: int = 0, limit: int = 50
    ) -> list[IdentityInDB]:
        """分页查询身份列表。"""
        cursor = (
            self._collection()
            .find()
            .sort("last_active_at", -1)
            .skip(skip)
            .limit(limit)
        )
        return [IdentityInDB.from_mongo(doc) async for doc in cursor]

    async def count(self) -> int:
        """统计身份总数。"""
        return await self._collection().count_documents({})

    async def search(self, q: str, limit: int = 50) -> list[IdentityInDB]:
        """按名称、昵称、平台ID或平台搜索身份。"""
        regex = {"$regex": q, "$options": "i"}
        cursor = (
            self._collection()
            .find({"$or": [{"name": regex}, {"nickname": regex}, {"platform_id": regex}, {"platform": regex}]})
            .sort("last_active_at", -1)
            .limit(limit)
        )
        return [IdentityInDB.from_mongo(doc) async for doc in cursor]

    async def count_by_account(self, account_id: str) -> int:
        """统计账号下身份数量。"""
        return await self._collection().count_documents({"account_id": account_id})

    async def set_blocked(
        self, identity_id: str, blocked: bool, reason: str = ""
    ) -> bool:
        """设置身份禁用/启用状态。"""
        result = await self._collection().update_one(
            {"id": identity_id},
            {"$set": {
                "is_blocked": blocked,
                "block_reason": reason if blocked else "",
            }},
        )
        return result.modified_count > 0

    async def update_last_active(self, identity_id: str) -> bool:
        """更新身份最后活跃时间为当前 UTC 时间。

        专用于在每轮对话处理完成后刷新 `last_active_at`，
        因 `UserUpdate` 模型不包含该字段，故走独立通道。
        """
        result = await self._collection().update_one(
            {"id": identity_id},
            {"$set": {"last_active_at": datetime.now(timezone.utc)}},
        )
        # 用 matched 而非 modified：BSON datetime 只到毫秒，同一毫秒内的重复刷新
        # 会让 modified_count 为 0，于是"刷新成功与否"取决于代码跑在多快的时钟上
        # （CI 与本地就曾因此一端绿一端红）。本方法的语义是"这个身份存在且已被打上
        # 新的活跃时间"，与值有没有真的变化无关；三处调用方也都只看是否抛异常。
        return result.matched_count > 0

    async def bind_platform_id(
        self,
        identity_id: str,
        platform_id: str,
        nickname: str = "",
        platform: Literal["qq", "wechat"] | None = None,
    ) -> None:
        """绑定真实平台 openid，并可同时写入首次外部平台类型。"""
        update_fields: dict = {
            "platform_id": platform_id,
            "last_active_at": datetime.now(timezone.utc),
        }
        if platform is not None:
            update_fields["platform"] = platform
        if nickname:
            update_fields["nickname"] = nickname
        await self._collection().update_one(
            {"id": identity_id},
            {"$set": update_fields},
        )

    async def reset_platform_binding(self, identity_id: str, bot_id: str) -> bool:
        """清除身份与指定 Bot 的平台绑定，保留身份行与历史数据。

        C 端删除渠道且该身份已无任何服务时调用，避免同一 openid 在新建渠道时
        命中旧身份而无法绑定；只清当前 Bot 的映射，身份本身不删除。
        """
        result = await self._collection().update_one(
            {"id": identity_id},
            {
                "$set": {
                    "platform_id": "",
                    "platform": None,
                    "last_active_at": datetime.now(timezone.utc),
                },
                "$unset": {f"bot_openids.{bot_id}": ""},
            },
        )
        return result.matched_count > 0

    async def bind_bot_openid(
        self, identity_id: str, bot_id: str, platform_id: str
    ) -> bool:
        """记录身份在指定 Bot（appid）下的 openid 映射。

        QQ 的 openid 按 appid 生成，同一身份在不同 Bot 下并不相同；
        主动消息发送时需按 Bot 路由到对应 openid，否则会因目标无效被平台拒绝。
        采用 `$ne` 过滤实现幂等写入（值未变化时不产生写操作）。

        Returns:
            是否实际发生了写入
        """
        if not identity_id or not bot_id or not platform_id:
            return False
        result = await self._collection().update_one(
            {"id": identity_id, f"bot_openids.{bot_id}": {"$ne": platform_id}},
            {"$set": {f"bot_openids.{bot_id}": platform_id}},
        )
        return result.modified_count > 0
