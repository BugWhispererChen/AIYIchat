"""账号额度数据模型。"""

from datetime import datetime, timezone
from typing import Optional, Self
from uuid import uuid4

from pydantic import BaseModel, Field


class QuotaEntry(BaseModel):
    """单项额度条目（如文字、语音、图片）。"""

    limit: int
    used: int = 0
    expires_at: Optional[datetime] = None


class AccountQuotaCreate(BaseModel):
    """创建账号额度记录。"""

    id: str = Field(default_factory=lambda: str(uuid4()))
    account_id: str
    text_quota: QuotaEntry
    voice_quota: QuotaEntry
    image_quota: QuotaEntry


class AccountQuotaInDB(AccountQuotaCreate):
    """数据库中的账号额度记录。"""

    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    model_config = {"populate_by_name": True}

    @classmethod
    def from_mongo(cls, doc: dict) -> Self:
        """从 MongoDB 文档构造额度对象。"""
        if doc is None:
            raise ValueError("Document is None")
        doc["_id"] = str(doc["_id"])
        return cls(**doc)


class AccountQuotaUpdate(BaseModel):
    """更新账号额度记录。"""

    text_quota: Optional[QuotaEntry] = None
    voice_quota: Optional[QuotaEntry] = None
    image_quota: Optional[QuotaEntry] = None
