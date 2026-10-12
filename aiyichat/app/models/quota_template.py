"""额度模板数据模型。"""

from datetime import datetime, timezone
from typing import Optional, Self

from pydantic import BaseModel, Field


class QuotaTemplateCreate(BaseModel):
    """创建额度模板。"""

    name: str
    text_limit: int
    voice_limit: int
    image_limit: int
    validity_days: Optional[int] = None


class QuotaTemplateInDB(BaseModel):
    """数据库中的额度模板。"""

    id: str = Field(alias="_id")
    name: str
    text_limit: int
    voice_limit: int
    image_limit: int
    validity_days: Optional[int] = None
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    model_config = {"populate_by_name": True}

    @classmethod
    def from_mongo(cls, doc: dict) -> Self:
        """从 MongoDB 文档构造模板对象。"""
        if doc is None:
            raise ValueError("Document is None")
        doc["_id"] = str(doc["_id"])
        return cls(**doc)


class QuotaTemplateUpdate(BaseModel):
    """更新额度模板。"""

    name: Optional[str] = None
    text_limit: Optional[int] = None
    voice_limit: Optional[int] = None
    image_limit: Optional[int] = None
    validity_days: Optional[int] = None
