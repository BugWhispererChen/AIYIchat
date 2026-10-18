"""C 端网页会话的数据库模型。

存 MongoDB 而非进程内存：支持 TTL 索引自动回收、并发上限控制与滑动过期。
"""

from datetime import datetime, timezone
from typing import Optional, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class WebSessionCreate(BaseModel):
    """创建网页会话。"""

    id: str = Field(default_factory=lambda: str(uuid4()))
    token: str  # secrets.token_urlsafe(32)
    credential_id: str  # 关联 web_credentials.id
    expires_at: datetime
    ip: str = ""
    user_agent: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class WebSessionInDB(WebSessionCreate):
    """数据库中的网页会话记录。"""

    model_config = ConfigDict(populate_by_name=True)

    @classmethod
    def from_mongo(cls, doc: dict) -> Self:
        """从 MongoDB 文档构造会话对象。"""
        if doc is None:
            raise ValueError("Document is None")
        doc["_id"] = str(doc["_id"])
        doc.setdefault("ip", "")
        doc.setdefault("user_agent", "")
        doc.setdefault("last_activity", doc.get("created_at", datetime.now(timezone.utc)))
        return cls(**doc)

    @property
    def expired(self) -> bool:
        """会话是否已过期（应用层判定，TTL 索引只负责异步回收）。"""
        expires_at = self.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at <= datetime.now(timezone.utc)

    def is_valid_for(self, credential_status: str) -> bool:
        """会话有效性判定：未过期且身份仍处于指定状态。

        管理员禁用/删除身份后，即使 token 未过期也必须立即失效，故状态由调用方传入
        而非在会话内缓存。
        """
        return not self.expired and credential_status == "active"

    @property
    def remaining_seconds(self) -> Optional[int]:
        """距过期的剩余秒数（已过期返回 None，供前端提示）。"""
        if self.expired:
            return None
        expires_at = self.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return max(0, int((expires_at - datetime.now(timezone.utc)).total_seconds()))
