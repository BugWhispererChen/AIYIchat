"""表情包数据模型。

表情包元数据存于 MongoDB stickers 集合，图片本体存于 media 集合，
此处仅通过 media_url（/api/media/{id}）引用。
"""

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StickerDocument(BaseModel):
    """单条表情包记录。

    Attributes:
        name: 名称/主关键词
        tags: 情绪/场景标签（用于无 embedding 时的关键词兜底匹配）
        description: 语义描述（生成 embedding 的来源文本）
        media_url: 媒体引用地址（/api/media/{id}）
        content_type: 原始媒体类型（GIF 保留原图以维持动画）
        content_hash: 图片内容 sha256，预置资源导入去重用
        source: 来源标识（ChineseBQB / upload 等）
        group_id: 所属分组 ID（空表示未归组，仅后台可见不参与发送）
        embedding: 语义向量（惰性生成，未配置向量化时为空）
        enabled: 是否启用（停用后不参与检索与发送）
        usage_count: 累计使用次数（运营观察热门表情用）
    """

    id: str = Field(default="", alias="_id")
    name: str = ""
    tags: list[str] = Field(default_factory=list)
    description: str = ""
    media_url: str = ""
    content_type: str = "image/gif"
    content_hash: str = ""
    source: str = ""
    group_id: str = ""
    embedding: list[float] = Field(default_factory=list)
    enabled: bool = True
    usage_count: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = ConfigDict(populate_by_name=True)

    @classmethod
    def from_mongo(cls, doc: dict[str, Any] | None) -> "StickerDocument | None":
        """将 MongoDB 文档转为模型实例（_id 转为 id 字符串）。"""
        if doc is None:
            return None
        doc["_id"] = str(doc["_id"])
        return cls(**doc)
