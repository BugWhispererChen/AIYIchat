from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field


class ConversationInDB(BaseModel):
    """数据库中的对话记录。"""

    id: str = Field(alias="_id")
    identity_id: str
    profile_id: str
    service_id: str  # 关联的对话服务ID
    title: str = ""
    summary: Optional[str] = None
    summary_message_count: int = 0  # 摘要已覆盖的消息数量
    # 摘要覆盖的时间范围：注入时渲染为“【之前的对话摘要（覆盖 X ~ Y）】”，
    # 使摘要具备时间坐标而非悬空文本（存量会话为 None 时退回无范围形式）
    summary_start_at: Optional[datetime] = None
    summary_end_at: Optional[datetime] = None
    message_count: int = 0
    status: Literal["active", "inactive"] = "active"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_message_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = {"populate_by_name": True}

    @classmethod
    def from_mongo(cls, doc: dict) -> "ConversationInDB":
        """从 MongoDB 文档构造对话对象。"""
        if doc is None:
            raise ValueError("Document is None")
        doc["_id"] = str(doc["_id"])
        doc["identity_id"] = str(doc["identity_id"])
        doc["profile_id"] = str(doc["profile_id"])
        # 兼容存量数据：无 service_id 时设为空字符串
        doc["service_id"] = str(doc.get("service_id", ""))
        # 兼容存量数据：无摘要时间范围字段时补 None（注入时退回无范围标题）
        doc.setdefault("summary_start_at", None)
        doc.setdefault("summary_end_at", None)
        # 兼容存量数据：无 updated_at 时使用 created_at
        if "updated_at" not in doc:
            doc["updated_at"] = doc.get("created_at", datetime.now(timezone.utc))
        return cls(**doc)
