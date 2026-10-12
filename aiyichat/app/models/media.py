from datetime import datetime, timezone

from pydantic import BaseModel, Field


class MediaFileInDB(BaseModel):
    """数据库中的媒体文件记录。"""

    id: str = Field(alias="_id")
    content_type: str  # "image/jpeg", "image/png", etc.
    filename: str
    size: int  # bytes
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = {"populate_by_name": True}

    @classmethod
    def from_mongo(cls, doc: dict) -> "MediaFileInDB":
        """从 MongoDB 文档构造媒体文件对象。"""
        if doc is None:
            raise ValueError("Document is None")
        doc["_id"] = str(doc["_id"])
        # data 字段不放进模型，按需单独读取
        doc.pop("data", None)
        # 兼容存量数据：无 updated_at 时使用 created_at
        if "updated_at" not in doc:
            doc["updated_at"] = doc.get("created_at", datetime.now(timezone.utc))
        return cls(**doc)
