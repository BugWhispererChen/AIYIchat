from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class NormalizedMessage:
    """标准化消息格式，平台无关"""
    platform: str          # "qq" / "wechat" / "web"
    # user_id 装的是平台下发的 openid（QQ 按 appid 生成、微信为 iLink uid），
    # 不是本项目 identities 集合的主键 identity_id。属外部协议标识，禁止改名。
    user_id: str           # 平台身份ID
    nickname: str          # 身份昵称
    content: str           # 消息文本
    message_id: str = ""   # 原始消息ID
    is_group: bool = False # 是否群消息
    group_id: str = ""     # 群ID（如果是群消息）
    bot_id: str = ""       # Bot ID（用于查找对话服务）
    # 平台下发的消息时间（UTC aware）：为后续从平台时间推断身份时区留口；
    # None 表示平台未下发或适配器未解析，落库时以服务端接收时间为准
    timestamp: Optional[datetime] = None


class ChannelAdapter(ABC):
    """消息平台适配器抽象接口"""

    @abstractmethod
    async def parse_incoming(self, raw_event: dict) -> Optional[NormalizedMessage]:
        """将平台原始事件解析为标准化消息"""
        ...

    @abstractmethod
    async def send_message(self, user_id: str, content: str, **kwargs) -> bool:
        """发送消息到平台"""
        ...
