"""微信引用消息的持久缓存。

缓存按 bot、会话与 svr_id 隔离，服务重启后仍可还原被引用正文和媒体信息。
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from app.db.mongo import get_database


class WeChatQuoteCacheRepository:
    """MongoDB 版引用缓存，替代官方插件的本地 SQLite 旁路存储。"""

    def _collection(self) -> Any:
        """返回微信引用缓存集合。"""
        return get_database().wechat_quote_cache

    @staticmethod
    def _cache_id(bot_id: str, session_key: str, svr_id: str) -> str:
        """生成确定性主键，重复投递只更新同一条记录。"""
        return f"{bot_id}:{session_key}:{svr_id}"

    async def upsert(
        self,
        *,
        bot_id: str,
        account_id: str,
        session_key: str,
        svr_id: str,
        message_id: str = "",
        role: str = "user",
        text: str = "",
        media_kind: str = "",
        media_url: str = "",
        retention_days: int = 30,
    ) -> None:
        """写入引用缓存；空 svr_id 不落库，避免产生不可命中的脏数据。"""
        if not bot_id or not session_key or not svr_id:
            return
        now = datetime.now(timezone.utc)
        await self._collection().update_one(
            {"_id": self._cache_id(bot_id, session_key, svr_id)},
            {
                "$set": {
                    "bot_id": bot_id,
                    "account_id": account_id,
                    "session_key": session_key,
                    "svr_id": svr_id,
                    "message_id": message_id,
                    "role": role,
                    "text": text[:20000],
                    "media_kind": media_kind,
                    "media_url": media_url,
                    "updated_at": now,
                    "expires_at": now + timedelta(days=retention_days),
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )

    async def find(
        self, *, bot_id: str, session_key: str, svr_id: str
    ) -> dict[str, Any] | None:
        """仅在同 Bot、同会话内还原，禁止跨账号串数据。"""
        if not bot_id or not session_key or not svr_id:
            return None
        doc = await self._collection().find_one({
            "bot_id": bot_id,
            "session_key": session_key,
            "svr_id": svr_id,
        })
        if not doc:
            return None
        expires_at = doc.get("expires_at")
        if isinstance(expires_at, datetime):
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at < datetime.now(timezone.utc):
                await self._collection().delete_one({"_id": doc["_id"]})
                return None
        return doc

    async def purge_expired(self, limit: int = 500) -> int:
        """惰性清理过期记录，避免引用缓存无限增长。"""
        result = await self._collection().delete_many({
            "expires_at": {"$lt": datetime.now(timezone.utc)}
        })
        return min(result.deleted_count, limit)

    async def delete_by_bot(self, bot_id: str) -> int:
        """渠道删除时清理该 Bot 的全部引用。"""
        result = await self._collection().delete_many({"bot_id": bot_id})
        return result.deleted_count

    async def delete_by_account(self, account_id: str) -> int:
        """账号注销时清理归属该账号的引用。"""
        result = await self._collection().delete_many({"account_id": account_id})
        return result.deleted_count

    async def delete_by_bot_ids(self, bot_ids: list[str]) -> int:
        """账号级联删除已预扫描出的 Bot ID 对应引用。"""
        if not bot_ids:
            return 0
        result = await self._collection().delete_many({"bot_id": {"$in": bot_ids}})
        return result.deleted_count
