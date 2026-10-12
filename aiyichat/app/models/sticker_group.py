"""表情包分组数据模型。

分组元数据存于 MongoDB sticker_groups 集合；每张表情包归属单一分组
（StickerDocument.group_id），角色通过 sticker_group_ids 绑定多个分组，
未绑定任何分组的角色不发送表情包。
"""

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StickerGroupDocument(BaseModel):
    """单条表情包分组记录。

    Attributes:
        name: 分组名称（唯一，按名称 upsert 供 seed 幂等）
        description: 分组说明（后台展示用）
        enabled: 是否启用（停用后该分组内表情不参与检索与发送）
        created_at: 创建时间（UTC）
    """

    id: str = Field(default="", alias="_id")
    name: str = ""
    description: str = ""
    enabled: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = ConfigDict(populate_by_name=True)

    @classmethod
    def from_mongo(cls, doc: dict[str, Any] | None) -> "StickerGroupDocument | None":
        """将 MongoDB 文档转为模型实例（_id 转为 id 字符串）。"""
        if doc is None:
            return None
        doc["_id"] = str(doc["_id"])
        doc.setdefault("description", "")
        doc.setdefault("enabled", True)
        return cls(**doc)
