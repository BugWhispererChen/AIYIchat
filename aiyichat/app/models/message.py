from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field


class MessageInDB(BaseModel):
    """数据库中的消息记录。"""

    id: str = Field(alias="_id")
    conversation_id: str
    # "user" 是 OpenAI 兼容协议的 role 取值，且已作为存量值写入 messages 集合并建索引
    # （db/mongo.py 的 messages.role 索引）。本项目把"用户"改称"身份"后，此字面量仍禁止改名。
    role: Literal["user", "assistant", "system"]
    content: str
    content_type: Literal["text", "voice", "image", "emoji"] = "text"  # 消息内容类型
    media_url: Optional[str] = None
    emotion_tag: Optional[str] = None
    token_count: Optional[int] = None
    llm_model: Optional[str] = None
    latency_ms: Optional[int] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = {"populate_by_name": True}

    @classmethod
    def from_mongo(cls, doc: dict) -> "MessageInDB":
        """从 MongoDB 文档构造消息对象。"""
        if doc is None:
            raise ValueError("Document is None")
        doc["_id"] = str(doc["_id"])
        doc["conversation_id"] = str(doc["conversation_id"])
        # 兼容存量数据：无 updated_at 时使用 created_at
        if "updated_at" not in doc:
            doc["updated_at"] = doc.get("created_at", datetime.now(timezone.utc))
        return cls(**doc)
