"""主动消息发送记录的数据库访问层。"""

from datetime import datetime, timedelta, timezone, tzinfo
from typing import Optional

from app.db.mongo import get_database
from app.models.proactive_record import ProactiveRecord, ProactiveRecordInDB


class ProactiveRecordRepository:
    """管理主动消息发送记录的增删改查。"""

    def _collection(self):
        """获取 MongoDB 集合。"""
        return get_database().proactive_records

    async def create(self, record: ProactiveRecord) -> str:
        """创建主动消息记录。"""
        doc = record.model_dump()
        doc["_id"] = doc.pop("id")
        await self._collection().insert_one(doc)
        return record.id

    async def mark_sent(self, record_id: str, content: str = "") -> None:
        """标记记录为已发送。"""
        update: dict = {
            "status": "sent",
            "sent_at": datetime.now(timezone.utc),
        }
        if content:
            update["content"] = content
        await self._collection().update_one(
            {"_id": record_id}, {"$set": update}
        )

    async def mark_failed(self, record_id: str, error_msg: str) -> None:
        """标记记录为发送失败。"""
        await self._collection().update_one(
            {"_id": record_id},
            {"$set": {"status": "failed", "error_msg": error_msg}},
        )

    async def find_latest_by_identity(
        self, identity_id: str, bot_id: str, limit: int = 10
    ) -> list[ProactiveRecordInDB]:
        """查询某身份最近的主动消息记录。"""
        cursor = (
            self._collection()
            .find({"identity_id": identity_id, "bot_id": bot_id})
            .sort("created_at", -1)
            .limit(limit)
        )
        results = []
        async for doc in cursor:
            results.append(ProactiveRecordInDB.from_mongo(doc))
        return results

    async def get_sent_stages(
        self, identity_id: str, bot_id: str, since: datetime
    ) -> list[int]:
        """
        获取指定时间之后已成功发送的阶段列表。

        用于判断哪些阶段已经发过，避免重复发送。
        """
        cursor = self._collection().find(
            {
                "identity_id": identity_id,
                "bot_id": bot_id,
                "status": "sent",
                "sent_at": {"$gte": since},
            },
            {"stage": 1},
        )
        stages = []
        async for doc in cursor:
            stages.append(doc["stage"])
        return stages

    async def get_attempted_stages(
        self,
        identity_id: str,
        bot_id: str,
        since: datetime,
        max_retries: int = 2,
    ) -> list[int]:
        """
        获取指定时间之后「已成功发送 / 已主动退让 / 失败次数达上限」的阶段列表。

        用于抑制重复触发：阶段一旦发出、被主动退让，或累计失败次数达到 max_retries，
        本轮活跃周期内就不再重新触发（否则每轮扫描都要重新调用 LLM 生成内容而耗尽额度）。
        skipped 必须计入，否则退让后每 15 分钟就重新判一次、重新"想发一次"，
        后台会被同一条退让记录刷满。

        Args:
            max_retries: 单阶段允许的最大失败重试次数，达到后不再尝试。
        """
        pipeline = [
            {
                "$match": {
                    "identity_id": identity_id,
                    "bot_id": bot_id,
                    "status": {"$in": ["sent", "failed", "skipped"]},
                    "created_at": {"$gte": since},
                }
            },
            {
                "$group": {
                    "_id": "$stage",
                    "sent": {
                        "$sum": {
                            "$cond": [{"$eq": ["$status", "sent"]}, 1, 0]
                        }
                    },
                    "skipped": {
                        "$sum": {
                            "$cond": [{"$eq": ["$status", "skipped"]}, 1, 0]
                        }
                    },
                    "failed": {
                        "$sum": {
                            "$cond": [{"$eq": ["$status", "failed"]}, 1, 0]
                        }
                    },
                }
            },
        ]
        stages: list[int] = []
        async for row in self._collection().aggregate(pipeline):
            # 已成功、已退让、或失败次数达上限 → 视为已尝试，抑制后续重试
            if (
                row["sent"] > 0
                or row.get("skipped", 0) > 0
                or row["failed"] >= max_retries
            ):
                stages.append(int(row["_id"]))
        return stages

    async def count_sent_this_month(
        self, identity_id: str, bot_id: str, platform: str
    ) -> int:
        """
        统计当月已发送的主动消息数（用于QQ每月4条配额追踪）。
        """
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        count = await self._collection().count_documents(
            {
                "identity_id": identity_id,
                "bot_id": bot_id,
                "platform": platform,
                "status": "sent",
                "sent_at": {"$gte": month_start},
            }
        )
        return count

    async def count_sent_today(
        self,
        identity_id: str,
        bot_id: str,
        platform: str,
        tz: tzinfo | None = None,
    ) -> int:
        """统计对方**本地今天**已收到几条主动消息（日限用，不含退让记录）。

        日界按对方时区而不是服务器时区：用 UTC 算"今天"，
        给海外对方判成"昨天没发过"，日限就形同虚设。
        """
        now_local = datetime.now(tz or timezone.utc)
        day_start = now_local.replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(timezone.utc)
        return await self._collection().count_documents(
            {
                "identity_id": identity_id,
                "bot_id": bot_id,
                "platform": platform,
                "status": "sent",
                "sent_at": {"$gte": day_start},
            }
        )

    async def list_recent_sent(
        self, identity_id: str, bot_id: str, limit: int = 4
    ) -> list[ProactiveRecordInDB]:
        """按发送时间倒序取最近几条**已发出**的主动消息（退让判定要逐条看有没有被回应）。"""
        cursor = (
            self._collection()
            .find(
                {
                    "identity_id": identity_id,
                    "bot_id": bot_id,
                    "status": "sent",
                    "sent_at": {"$ne": None},
                }
            )
            .sort("sent_at", -1)
            .limit(limit)
        )
        return [ProactiveRecordInDB.from_mongo(doc) async for doc in cursor]

    async def get_latest_sent(
        self, identity_id: str, bot_id: str
    ) -> Optional[ProactiveRecordInDB]:
        """获取最近一条已发送的主动消息记录（用于去重）。"""
        doc = await self._collection().find_one(
            {"identity_id": identity_id, "bot_id": bot_id, "status": "sent"},
            sort=[("created_at", -1)],
        )
        if doc:
            return ProactiveRecordInDB.from_mongo(doc)
        return None

    async def find_by_id(self, record_id: str) -> Optional[ProactiveRecordInDB]:
        """根据ID查询记录。"""
        doc = await self._collection().find_one({"_id": record_id})
        if doc is None:
            return None
        return ProactiveRecordInDB.from_mongo(doc)

    # ── 记录清理（后台定量删除老旧记录）─────────────

    @staticmethod
    def _older_than_query(days: int, only_failed: bool) -> dict:
        """构造“创建时间早于 N 天前”的查询条件。

        days 下限为 1，避免传入 0/负数时误删全量记录。
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
        query: dict = {"created_at": {"$lt": cutoff}}
        if only_failed:
            query["status"] = "failed"
        return query

    async def count_older_than(
        self, days: int, only_failed: bool = False
    ) -> int:
        """统计早于 N 天前的记录数（清理前预览影响面）。"""
        return await self._collection().count_documents(
            self._older_than_query(days, only_failed)
        )

    async def delete_older_than(
        self, days: int, only_failed: bool = False
    ) -> int:
        """删除早于 N 天前的记录，返回删除条数。

        走 created_at 索引；only_failed 时仅清理失败记录，
        保留成功记录以不影响月配额与阶段已发送判定。
        """
        result = await self._collection().delete_many(
            self._older_than_query(days, only_failed)
        )
        return result.deleted_count

    async def delete_by_id(self, record_id: str) -> bool:
        """删除单条记录（清理误发/测试记录）。"""
        result = await self._collection().delete_one({"_id": record_id})
        return result.deleted_count > 0

    async def delete_by_identity(self, identity_id: str) -> int:
        """删除某身份的全部主动消息记录（用户自助注销时调用）。

        记录里存着实际发送出去的消息正文，属于用户可见数据，
        不能只靠账号级联删除漏掉它。
        """
        if not identity_id:
            return 0
        result = await self._collection().delete_many({"identity_id": identity_id})
        return result.deleted_count

    # ── 管理面板：全局分页与统计 ───────────────────────────

    async def list_recent(
        self, skip: int = 0, limit: int = 30, status: Optional[str] = None
    ) -> list[ProactiveRecordInDB]:
        """全局分页查询主动消息记录（按创建时间倒序）。"""
        query: dict = {}
        if status:
            query["status"] = status
        cursor = (
            self._collection()
            .find(query)
            .sort("created_at", -1)
            .skip(skip)
            .limit(limit)
        )
        return [ProactiveRecordInDB.from_mongo(doc) async for doc in cursor]

    async def count_all(self, status: Optional[str] = None) -> int:
        """统计主动消息记录总数（可按状态过滤）。"""
        query: dict = {}
        if status:
            query["status"] = status
        return await self._collection().count_documents(query)

    async def stats_summary(self, days: int = 7) -> dict:
        """近 N 天主动消息统计：总数/成功/失败/退让/成功率 + 按阶段分布。

        成功率分母只算"真的尝试发过"的（sent+failed）：退让是主动忍住，
        把它计入失败会把"学会不打扰"错误地显示成系统在坏。
        """
        since = datetime.now(timezone.utc) - timedelta(days=days)
        base = {"created_at": {"$gte": since}}
        total = await self._collection().count_documents(base)
        sent = await self._collection().count_documents(
            {**base, "status": "sent"}
        )
        failed = await self._collection().count_documents(
            {**base, "status": "failed"}
        )
        skipped = await self._collection().count_documents(
            {**base, "status": "skipped"}
        )
        attempts = sent + failed
        success_rate = round(sent / attempts * 100, 1) if attempts else 0.0

        # 按阶段分布（0=事件驱动，1-4=阶段策略）
        stage_pipeline = [
            {"$match": {**base, "status": "sent"}},
            {"$group": {"_id": "$stage", "count": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
        ]
        by_stage: dict[int, int] = {}
        async for row in self._collection().aggregate(stage_pipeline):
            by_stage[int(row["_id"])] = int(row["count"])

        return {
            "total": total,
            "sent": sent,
            "failed": failed,
            "skipped": skipped,
            "success_rate": success_rate,
            "by_stage": by_stage,
        }
