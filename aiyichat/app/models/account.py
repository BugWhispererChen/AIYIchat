"""账号的数据库模型。"""

from datetime import datetime, timezone
from typing import Self
from uuid import uuid4

from pydantic import BaseModel, Field

from app.models.quota import QuotaEntry


class AccountCreate(BaseModel):
    """创建账号。"""

    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    remark: str = ""


class AccountInDB(AccountCreate):
    """数据库中的账号记录。"""

    text_quota: QuotaEntry = Field(default_factory=lambda: QuotaEntry(limit=10**9, used=0))
    voice_quota: QuotaEntry = Field(default_factory=lambda: QuotaEntry(limit=10**9, used=0))
    image_quota: QuotaEntry = Field(default_factory=lambda: QuotaEntry(limit=10**9, used=0))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = {"populate_by_name": True}

    @classmethod
    def from_mongo(cls, doc: dict) -> Self:
        """从 MongoDB 文档构造账号对象。"""
        if doc is None:
            raise ValueError("Document is None")
        doc["_id"] = str(doc["_id"])
        return cls(**doc)


class AccountUpdate(BaseModel):
    """更新账号信息。"""

    name: str | None = None
    remark: str | None = None
