"""表情包发送任务的结构化描述。

独立小模块，供 engine / sticker_service / 适配器共享，避免循环导入。
"""

from dataclasses import dataclass


@dataclass
class StickerTask:
    """单个表情包发送任务。

    Attributes:
        description: 触发本任务的语义描述（LLM [表情:xxx] 标签内容或斗图触发说明）
        sticker_id: 命中的表情包记录 ID（空表示未检索到，不发送）
        media_url: 表情包媒体地址（/api/media/{id}）
        content_type: 媒体类型（GIF 发送时需跳过压缩以保留动画）
    """

    description: str
    sticker_id: str = ""
    media_url: str = ""
    content_type: str = "image/gif"
