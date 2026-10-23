"""Bot 配置的数据库访问层。"""

from datetime import datetime, timezone
from typing import Optional

from app.db.mongo import get_database
from app.models.bot_config import BotConfigCreate, BotConfigInDB, BotConfigUpdate


class BotConfigRepository:
    """管理 Bot 配置的增删改查。"""

    def _collection(self):
        """获取 MongoDB 集合。"""
        return get_database().bot_configs

    async def list_all(self) -> list[BotConfigInDB]:
        """查询所有 Bot 配置。"""
        cursor = self._collection().find().sort("name", 1)
        return [BotConfigInDB.from_mongo(doc) async for doc in cursor]

    async def list_by_account(self, account_id: str) -> list[BotConfigInDB]:
        """查询账号下的 Bot 配置。"""
        cursor = (
            self._collection()
            .find({"account_id": account_id})
            .sort("name", 1)
        )
        return [BotConfigInDB.from_mongo(doc) async for doc in cursor]

    async def list_by_platform(self, platform: str) -> list[BotConfigInDB]:
        """按平台筛选 Bot 配置。"""
        cursor = (
            self._collection()
            .find({"platform": platform})
            .sort("name", 1)
        )
        return [BotConfigInDB.from_mongo(doc) async for doc in cursor]

    async def count_by_account(self, account_id: str) -> int:
        """统计账号下的 Bot 数量。"""
        return await self._collection().count_documents({"account_id": account_id})

    async def find_by_app_id(
        self, platform: str, app_id: str
    ) -> Optional[BotConfigInDB]:
        """根据平台和 app_id 查询 Bot 配置。"""
        doc = await self._collection().find_one(
            {"platform": platform, "app_id": app_id}
        )
        if doc is None:
            return None
        return BotConfigInDB.from_mongo(doc)

    async def find_by_id(self, config_id: str) -> Optional[BotConfigInDB]:
        """根据 ID 查询 Bot 配置。"""
        doc = await self._collection().find_one({"id": config_id})
        if doc is None:
            return None
        return BotConfigInDB.from_mongo(doc)

    async def create(self, config: BotConfigCreate) -> BotConfigInDB:
        """创建 Bot 配置。"""
        now = datetime.now(timezone.utc)
        doc = config.model_dump()
        doc["created_at"] = now
        doc["updated_at"] = now
        result = await self._collection().insert_one(doc)
        doc["_id"] = result.inserted_id
        return BotConfigInDB.from_mongo(doc)

    async def update(
        self, config_id: str, update: BotConfigUpdate
    ) -> Optional[BotConfigInDB]:
        """更新 Bot 配置（按 id 查找）。"""
        update_data = update.model_dump(exclude_none=True)
        if not update_data:
            return await self.find_by_id(config_id)
        update_data["updated_at"] = datetime.now(timezone.utc)
        await self._collection().update_one(
            {"id": config_id}, {"$set": update_data}
        )
        return await self.find_by_id(config_id)

    async def delete(self, config_id: str) -> bool:
        """删除 Bot 配置（按 id 查找）。"""
        result = await self._collection().delete_one({"id": config_id})
        return result.deleted_count > 0

    async def delete_by_account(self, account_id: str) -> int:
        """删除账号下的所有 Bot 配置。"""
        result = await self._collection().delete_many({"account_id": account_id})
        return result.deleted_count

    async def count(self) -> int:
        """统计 Bot 配置总数。"""
        return await self._collection().count_documents({})

    async def update_wechat_token(
        self, config_id: str, context_token: str, expire_at: datetime
    ) -> bool:
        """更新微信 Bot 的 context_token（按 id 查找）。"""
        result = await self._collection().update_one(
            {"id": config_id},
            {
                "$set": {
                    "context_token": context_token,
                    "token_expire_at": expire_at,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        return result.modified_count > 0

    async def update_wechat_bot_token(
        self, config_id: str, bot_token: str
    ) -> bool:
        """更新微信 Bot 的 bot_token（按 id 查找）。"""
        result = await self._collection().update_one(
            {"id": config_id},
            {
                "$set": {
                    "bot_token": bot_token,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        return result.modified_count > 0

    async def update_status(self, config_id: str, status: str) -> bool:
        """更新 Bot 状态（按 id 查找）。"""
        result = await self._collection().update_one(
            {"id": config_id},
            {
                "$set": {
                    "status": status,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        return result.modified_count > 0

    async def clear_wechat_auth(self, config_id: str) -> bool:
        """清空微信登录态（退出登录），保留渠道配置本身。"""
        result = await self._collection().update_one(
            {"id": config_id},
            {
                "$set": {
                    "bot_token": "",
                    "context_token": "",
                    "status": "offline",
                    "updated_at": datetime.now(timezone.utc),
                },
                "$unset": {"token_expire_at": ""},
            },
        )
        return result.matched_count > 0

    async def update_qrcode(self, config_id: str, qrcode_url: str) -> bool:
        """更新微信登录二维码 URL（按 id 查找）。"""
        result = await self._collection().update_one(
            {"id": config_id},
            {
                "$set": {
                    "qrcode_url": qrcode_url,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        return result.modified_count > 0

    async def set_blocked(
        self, config_id: str, blocked: bool, reason: str = ""
    ) -> bool:
        """设置 Bot 禁用/启用状态。"""
        result = await self._collection().update_one(
            {"id": config_id},
            {"$set": {
                "is_blocked": blocked,
                "block_reason": reason if blocked else "",
                "updated_at": datetime.now(timezone.utc),
            }},
        )
        return result.modified_count > 0
