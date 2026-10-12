"""聊天记录分析任务的数据库访问层。

将 AnalysisTask 持久化到 analysis_tasks 集合（_id = task_id），
使分析任务在容器重启后仍可查询进度与结果。
"""

import logging
from datetime import datetime, timezone

from app.chat_import.models import AnalysisTask
from app.db.mongo import get_database

logger = logging.getLogger(__name__)


class AnalysisTaskRepository:
    """管理聊天记录分析任务的持久化。"""

    def _collection(self):
        """获取 MongoDB 集合。"""
        return get_database().analysis_tasks

    async def upsert(self, task: AnalysisTask) -> None:
        """写入或更新任务状态（以 task_id 为主键）。"""
        doc = task.model_dump()
        doc["updated_at"] = datetime.now(timezone.utc)
        await self._collection().update_one(
            {"_id": task.task_id},
            {"$set": doc},
            upsert=True,
        )

    async def find_by_id(self, task_id: str) -> AnalysisTask | None:
        """按任务ID查询，不存在返回 None。"""
        doc = await self._collection().find_one({"_id": task_id})
        if doc is None:
            return None
        doc.pop("_id", None)
        doc.pop("updated_at", None)
        return AnalysisTask(**doc)

    async def list_recent(self, limit: int = 20) -> list[AnalysisTask]:
        """按更新时间倒序列出最近的分析任务。"""
        cursor = (
            self._collection()
            .find({})
            .sort("updated_at", -1)
            .limit(limit)
        )
        tasks: list[AnalysisTask] = []
        async for doc in cursor:
            doc.pop("_id", None)
            doc.pop("updated_at", None)
            tasks.append(AnalysisTask(**doc))
        return tasks
