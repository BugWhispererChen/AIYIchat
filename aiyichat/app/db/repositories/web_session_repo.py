"""C 端网页会话的数据库访问层。"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from app.db.mongo import get_database
from app.models.web_session import WebSessionCreate, WebSessionInDB


class WebSessionRepository:
    """管理网页会话令牌的签发、校验、滑动过期与并发上限。"""

    def _collection(self):
        """获取 MongoDB 集合。"""
        return get_database().web_sessions

    async def create(self, session: WebSessionCreate) -> WebSessionInDB:
        """写入新会话。"""
        doc = session.model_dump()
        result = await self._collection().insert_one(doc)
        doc["_id"] = result.inserted_id
        return WebSessionInDB.from_mongo(doc)

    async def find_by_token(self, token: str) -> Optional[WebSessionInDB]:
        """按令牌查询会话。"""
        if not token:
            return None
        doc = await self._collection().find_one({"token": token})
        if doc is None:
            return None
        return WebSessionInDB.from_mongo(doc)

    async def touch(self, token: str, session_hours: int) -> bool:
        """滑动过期：每次成功访问刷新 last_activity 与 expires_at。"""
        now = datetime.now(timezone.utc)
        result = await self._collection().update_one(
            {"token": token},
            {
                "$set": {
                    "last_activity": now,
                    "expires_at": now + timedelta(hours=session_hours),
                }
            },
        )
        return result.modified_count > 0

    async def delete_by_token(self, token: str) -> bool:
        """注销单个会话。"""
        result = await self._collection().delete_one({"token": token})
        return result.deleted_count > 0

    async def delete_by_identity(self, identity_id: str) -> int:
        """清空某身份的全部会话（封禁、改密、管理员强制下线时调用）。"""
        result = await self._collection().delete_many({"identity_id": identity_id})
        return result.deleted_count

    async def list_by_identity(self, identity_id: str) -> list[WebSessionInDB]:
        """列出某身份的有效会话，按创建时间升序（最旧在前）。"""
        cursor = (
            self._collection()
            .find({"identity_id": identity_id})
            .sort("created_at", 1)
        )
        return [WebSessionInDB.from_mongo(doc) async for doc in cursor]

    async def enforce_max_concurrent(self, identity_id: str, max_concurrent: int) -> int:
        """并发上限控制：超出上限时按创建时间踢出最旧会话，返回被踢数量。

        在新会话签发之后调用，故"登录即挤掉自己最旧的一个设备"，
        同时天然实现"旧 token 立即失效"的会话固定防护。
        """
        sessions = await self.list_by_identity(identity_id)
        if max_concurrent < 1:
            max_concurrent = 1
        excess = sessions[: max(0, len(sessions) - max_concurrent)]
        removed = 0
        for s in excess:
            if await self.delete_by_token(s.token):
                removed += 1
        return removed
