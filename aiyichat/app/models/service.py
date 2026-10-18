"""对话服务的数据库模型。"""

from datetime import datetime, timezone
from typing import Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class ServiceCreate(BaseModel):
    """创建对话服务。"""

    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = ""
    account_id: str
    identity_id: str
    bot_id: str
    profile_id: str
    is_active: bool = True  # 新建服务默认启用
    disable_reason: str = ""  # 禁用原因
    share_group_id: str = ""  # 共享组ID：非空时同组服务共享对话与长期记忆
    timezone: str = ""  # IANA 时区名（如 Asia/Shanghai），空串回落身份级/全局默认偏移


class ServiceInDB(ServiceCreate):
    """数据库中的对话服务记录。"""

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = ConfigDict(
        populate_by_name=True,
    )

    @classmethod
    def from_mongo(cls, doc: dict) -> Self:
        """从 MongoDB 文档构造服务对象。"""
        if doc is None:
            raise ValueError("Document is None")
        doc["_id"] = str(doc["_id"])
        # 兼容存量数据：无 updated_at 时使用 created_at；无 disable_reason / is_active 时用默认值
        if "updated_at" not in doc:
            doc["updated_at"] = doc.get("created_at", datetime.now(timezone.utc))
        doc.setdefault("disable_reason", "")
        doc.setdefault("is_active", True)
        doc.setdefault("share_group_id", "")
        doc.setdefault("timezone", "")
        return cls(**doc)

    @property
    def context_key(self) -> str:
        """对话/记忆的归属 key：设置共享组时用组 ID，否则用服务自身 ID。"""
        return self.share_group_id or self.id


class ServiceUpdate(BaseModel):
    """更新对话服务。"""

    name: str | None = None
    account_id: str | None = None
    identity_id: str | None = None
    bot_id: str | None = None
    profile_id: str | None = None
    is_active: bool | None = None
    disable_reason: str | None = None
    share_group_id: str | None = None
    timezone: str | None = None  # IANA 时区名，空串回落身份级/全局默认偏移
