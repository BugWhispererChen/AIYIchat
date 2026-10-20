"""内容安全事件的数据库访问层。"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from app.db.mongo import get_database
from app.models.safety_event import SafetyEvent, SafetyEventInDB


class SafetyEventRepository:
    """管理内容安全事件的写入与查询。"""

    def _collection(self):
        """获取 MongoDB 集合。"""
        return get_database().safety_events

    async def create(self, event: SafetyEvent) -> str:
        """写入一条安全事件记录。"""
        doc = event.model_dump()
        doc["_id"] = doc.pop("id")
        await self._collection().insert_one(doc)
        return event.id

    async def list_recent(
        self,
        skip: int = 0,
        limit: int = 50,
        event_type: Optional[str] = None,
    ) -> list[SafetyEventInDB]:
        """分页查询最近的安全事件（按时间倒序）。"""
        query: dict = {}
        if event_type:
            query["event_type"] = event_type
        cursor = (
            self._collection()
            .find(query)
            .sort("created_at", -1)
            .skip(skip)
            .limit(limit)
        )
        return [SafetyEventInDB.from_mongo(doc) async for doc in cursor]

    async def count(self, event_type: Optional[str] = None) -> int:
        """统计安全事件总数（可按类型过滤）。"""
        query: dict = {}
        if event_type:
            query["event_type"] = event_type
        return await self._collection().count_documents(query)

    async def delete_by_account(self, account_id: str) -> int:
        """删除某账号的安全事件记录（用户自助注销时调用）。

        事件里带身份输入摘要（excerpt）与 platform_id，属个人数据；
        账号注销后留着它们既无审计对象也无归属账号。
        """
        if not account_id:
            return 0
        result = await self._collection().delete_many({"account_id": account_id})
        return result.deleted_count

    async def count_recent_by_type(self, days: int = 7) -> dict[str, int]:
        """统计近 N 天各类型事件数量，返回 {event_type: count}。"""
        since = datetime.now(timezone.utc) - timedelta(days=days)
        pipeline = [
            {"$match": {"created_at": {"$gte": since}}},
            {"$group": {"_id": "$event_type", "count": {"$sum": 1}}},
        ]
        result: dict[str, int] = {}
        async for row in self._collection().aggregate(pipeline):
            result[str(row["_id"])] = int(row["count"])
        return result
