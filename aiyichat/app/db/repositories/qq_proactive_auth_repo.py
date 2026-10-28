"""QQ 主动消息平台授权状态的数据库访问层。"""

from datetime import datetime, timezone
from typing import Any

from app.db.mongo import get_database


class QQProactiveAuthRepository:
    """按 Bot 与 C2C/群 openid 持久化主动消息授权。"""

    def _collection(self) -> Any:
        """获取 QQ 专属授权集合。"""
        return get_database().qq_proactive_authorizations

    async def upsert(
        self,
        bot_id: str,
        target_type: str,
        target_id: str,
        allowed: bool,
        event_type: str,
    ) -> None:
        """以确定性主键幂等更新授权，避免依赖全局索引变更。"""
        if target_type not in ("c2c", "group"):
            raise ValueError(f"不支持的 QQ 授权目标类型: {target_type}")
        if not bot_id or not target_id:
            raise ValueError("QQ 授权缺少 bot_id 或 target_id")
        now = datetime.now(timezone.utc)
        await self._collection().update_one(
            {"_id": f"{bot_id}:{target_type}:{target_id}"},
            {
                "$set": {
                    "bot_id": bot_id,
                    "platform": "qq",
                    "target_type": target_type,
                    "target_id": target_id,
                    "allowed": allowed,
                    "event_type": event_type,
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )

    async def list_by_bot(self, bot_id: str) -> list[dict[str, Any]]:
        """读取单个 Bot 的全部授权，供客户端启动时恢复。"""
        cursor = self._collection().find({"bot_id": bot_id, "platform": "qq"})
        return [doc async for doc in cursor]
