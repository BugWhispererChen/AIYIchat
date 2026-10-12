"""用户与 Bot 绑定关系的数据库模型。"""

from datetime import datetime, timezone
from typing import Self

from pydantic import BaseModel, Field


class UserBotBindingCreate(BaseModel):
    """创建用户与 Bot 的绑定关系。"""

    user_id: str
    bot_id: str


class UserBotBindingInDB(BaseModel):
    """数据库中的用户与 Bot 绑定关系。"""

    id: str = Field(alias="_id")
    user_id: str
    bot_id: str
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    model_config = {"populate_by_name": True}

    @classmethod
    def from_mongo(cls, doc: dict) -> Self:
        """从 MongoDB 文档构造绑定对象。"""
        if doc is None:
            raise ValueError("Document is None")
        doc["_id"] = str(doc["_id"])
        # 兼容存量数据：无 updated_at 时使用 created_at
        if "updated_at" not in doc:
            doc["updated_at"] = doc.get("created_at", datetime.now(timezone.utc))
        return cls(**doc)
