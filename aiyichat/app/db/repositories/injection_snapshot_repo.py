"""记忆注入快照的数据库访问层。

管理 injection_snapshots 集合的写入与查询。
快照为审计数据，仅追加写入与只读查询，提供按服务/会话的检索与容量控制。
"""

import logging
from datetime import datetime, timezone

from app.db.mongo import get_database
from app.models.injection_snapshot import InjectionSnapshotInDB

logger = logging.getLogger(__name__)

# 每个服务最多保留的快照条数（超出后淘汰最旧，避免无限增长）
_MAX_SNAPSHOTS_PER_SERVICE = 100


class InjectionSnapshotRepository:
    """管理记忆注入快照的数据访问。"""

    def _collection(self):
        """获取 MongoDB 集合。"""
        return get_database().injection_snapshots

    async def save(self, snapshot: InjectionSnapshotInDB) -> None:
        """保存一条注入快照（fire-and-forget 语义，内部吃掉异常）。

        写入后按服务做容量控制，仅保留最近 _MAX_SNAPSHOTS_PER_SERVICE 条。
        """
        try:
            doc = snapshot.model_dump(by_alias=True, exclude={"id"})
            await self._collection().insert_one(doc)
            await self._trim_service_snapshots(snapshot.service_id)
        except Exception as e:  # noqa: BLE001 快照写入失败不影响主链路
            logger.warning("注入快照写入失败: %s", e)

    async def _trim_service_snapshots(self, service_id: str) -> None:
        """按服务淘汰最旧快照，控制单服务快照数量上限。"""
        try:
            cursor = self._collection().find(
                {"service_id": service_id}
            ).sort("created_at", -1)
            docs = [d async for d in cursor]
            if len(docs) <= _MAX_SNAPSHOTS_PER_SERVICE:
                return
            stale_ids = [d["_id"] for d in docs[_MAX_SNAPSHOTS_PER_SERVICE:]]
            if stale_ids:
                await self._collection().delete_many({"_id": {"$in": stale_ids}})
        except Exception as e:  # noqa: BLE001
            logger.warning("快照容量控制失败: %s", e)

    async def list_by_service(
        self, service_id: str, limit: int = 50
    ) -> list[InjectionSnapshotInDB]:
        """按服务查询最近快照（按时间倒序）。"""
        cursor = (
            self._collection()
            .find({"service_id": service_id})
            .sort("created_at", -1)
            .limit(limit)
        )
        result: list[InjectionSnapshotInDB] = []
        async for doc in cursor:
            result.append(InjectionSnapshotInDB.from_mongo(doc))
        return result

    async def list_recent(self, limit: int = 100) -> list[InjectionSnapshotInDB]:
        """查询全局最近快照（按时间倒序），用于总览。"""
        cursor = self._collection().find({}).sort("created_at", -1).limit(limit)
        result: list[InjectionSnapshotInDB] = []
        async for doc in cursor:
            result.append(InjectionSnapshotInDB.from_mongo(doc))
        return result

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)
