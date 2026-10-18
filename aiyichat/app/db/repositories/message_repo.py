from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId

from app.db.mongo import get_database
from app.models.message import MessageInDB

# 北京时区偏移（用于按天分组时对齐身份本地日期）
_BEIJING_OFFSET_HOURS = 8

# 时间相同时以 _id 兜底排序：存量数据中同轮 user/assistant 的 created_at 可能精确相等，
# ObjectId 按插入顺序单调递增，可保证身份消息排在回复之前
_ORDER_ASC = [("created_at", 1), ("_id", 1)]
_ORDER_DESC = [("created_at", -1), ("_id", -1)]


class MessageRepository:
    """管理消息的增删改查。"""

    def _collection(self):
        """获取 MongoDB 集合。"""
        return get_database().messages

    async def create(
        self,
        conversation_id: str,
        role: str,
        content: str,
        emotion_tag: Optional[str] = None,
        token_count: Optional[int] = None,
        llm_model: Optional[str] = None,
        latency_ms: Optional[int] = None,
        content_type: str = "text",
        media_url: Optional[str] = None,
        created_at: Optional[datetime] = None,
    ) -> MessageInDB:
        """创建新消息。

        created_at 由调用方传入消息真实发生时刻（如身份消息的接收时间），
        缺省取当前时间；updated_at 始终为写入时间。
        """
        now = datetime.now(timezone.utc)
        doc = {
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
            "emotion_tag": emotion_tag,
            "token_count": token_count,
            "llm_model": llm_model,
            "latency_ms": latency_ms,
            "content_type": content_type,
            "media_url": media_url,
            "created_at": created_at or now,
            "updated_at": now,
        }
        result = await self._collection().insert_one(doc)
        doc["_id"] = result.inserted_id
        return MessageInDB.from_mongo(doc)

    async def get_recent(
        self, conversation_id: str, limit: int = 20
    ) -> list[MessageInDB]:
        """获取对话的最近消息（按时间正序）。"""
        cursor = (
            self._collection()
            .find({"conversation_id": conversation_id})
            .sort(_ORDER_DESC)
            .limit(limit)
        )
        messages = [MessageInDB.from_mongo(doc) async for doc in cursor]
        messages.reverse()  # 按时间正序
        return messages

    async def list_paginated(
        self, conversation_id: str, skip: int = 0, limit: int = 50
    ) -> list[MessageInDB]:
        """分页获取对话消息（按时间倒序取出后反转为正序显示）。"""
        cursor = (
            self._collection()
            .find({"conversation_id": conversation_id})
            .sort(_ORDER_DESC)
            .skip(skip)
            .limit(limit)
        )
        messages = [MessageInDB.from_mongo(doc) async for doc in cursor]
        messages.reverse()  # 反转为时间正序展示
        return messages

    async def count_by_conversation(self, conversation_id: str) -> int:
        """统计对话下的消息数量。"""
        return await self._collection().count_documents(
            {"conversation_id": conversation_id}
        )

    async def get_all_for_conversation(self, conversation_id: str) -> list[MessageInDB]:
        """获取对话下的所有消息（按时间正序）。"""
        cursor = (
            self._collection()
            .find({"conversation_id": conversation_id})
            .sort(_ORDER_ASC)
        )
        return [MessageInDB.from_mongo(doc) async for doc in cursor]

    async def count_total(self) -> int:
        """统计消息总数。"""
        return await self._collection().count_documents({})

    async def delete(self, message_id: str) -> bool:
        """删除指定消息。"""
        result = await self._collection().delete_one(
            {"_id": ObjectId(message_id)}
        )
        return result.deleted_count > 0

    async def delete_by_conversation(self, conversation_id: str) -> int:
        """删除某对话下的所有消息。"""
        result = await self._collection().delete_many(
            {"conversation_id": conversation_id}
        )
        return result.deleted_count

    async def count_today(self) -> int:
        """统计今日消息数量。"""
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return await self._collection().count_documents(
            {"created_at": {"$gte": today_start}}
        )

    async def delete_by_conversation_ids(self, conv_ids: list[str]) -> int:
        """批量删除多个对话下的所有消息。"""
        if not conv_ids:
            return 0
        result = await self._collection().delete_many(
            {"conversation_id": {"$in": conv_ids}}
        )
        return result.deleted_count

    # ── 经营数据聚合（仪表盘趋势图）────────────────────────────

    async def count_daily(self, days: int = 14) -> list[dict]:
        """按天（北京时区）统计消息量，返回 [{date: 'MM-DD', count: int}]（补零缺失日）。"""
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        since = (now - timedelta(days=days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        tz_offset = timedelta(hours=_BEIJING_OFFSET_HOURS)
        pipeline = [
            {"$match": {"created_at": {"$gte": since - tz_offset}}},
            {
                "$group": {
                    "_id": {
                        "$dateToString": {
                            "format": "%m-%d",
                            "date": "$created_at",
                            "timezone": "+08:00",
                        }
                    },
                    "count": {"$sum": 1},
                }
            },
        ]
        raw: dict[str, int] = {}
        async for row in self._collection().aggregate(pipeline):
            raw[str(row["_id"])] = int(row["count"])
        # 生成连续日期序列并补零，保证前端折线图 X 轴完整
        result: list[dict] = []
        for i in range(days):
            day = since + timedelta(days=i) + tz_offset
            label = day.strftime("%m-%d")
            result.append({"date": label, "count": raw.get(label, 0)})
        return result

    async def emotion_distribution(self, days: int = 7) -> list[dict]:
        """统计近 N 天身份消息的情绪分布，返回 [{emotion, count}]（降序）。"""
        from datetime import timedelta

        since = datetime.now(timezone.utc) - timedelta(days=days)
        pipeline = [
            {
                "$match": {
                    "created_at": {"$gte": since},
                    "role": "user",
                    "emotion_tag": {"$nin": [None, ""]},
                }
            },
            {"$group": {"_id": "$emotion_tag", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
        ]
        result: list[dict] = []
        async for row in self._collection().aggregate(pipeline):
            result.append({"emotion": str(row["_id"]), "count": int(row["count"])})
        return result

    async def avg_latency_daily(self, days: int = 14) -> list[dict]:
        """按天统计 AI 回复平均延迟（毫秒），返回 [{date: 'MM-DD', avg_ms: int}]。"""
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        since = (now - timedelta(days=days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        tz_offset = timedelta(hours=_BEIJING_OFFSET_HOURS)
        pipeline = [
            {
                "$match": {
                    "created_at": {"$gte": since - tz_offset},
                    "role": "assistant",
                    "latency_ms": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": {
                        "$dateToString": {
                            "format": "%m-%d",
                            "date": "$created_at",
                            "timezone": "+08:00",
                        }
                    },
                    "avg_ms": {"$avg": "$latency_ms"},
                }
            },
        ]
        raw: dict[str, float] = {}
        async for row in self._collection().aggregate(pipeline):
            raw[str(row["_id"])] = float(row["avg_ms"])
        result: list[dict] = []
        for i in range(days):
            day = since + timedelta(days=i) + tz_offset
            label = day.strftime("%m-%d")
            result.append({"date": label, "avg_ms": int(raw.get(label, 0))})
        return result
