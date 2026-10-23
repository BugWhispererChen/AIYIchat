"""
多 Bot 配置管理。

从 MongoDB 加载 Bot 配置。
提供异步读取接口。
"""

import logging

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class BotConfig(BaseModel):
    """单个 Bot 的运行时配置（QQ / 微信 / 网页）"""

    id: str  # Bot UUID 唯一标识
    name: str = ""  # Bot 显示名称
    platform: str = "qq"  # "qq" | "wechat" | "web"
    app_id: str = ""
    app_secret: str = ""
    sandbox_mode: bool = True
    is_blocked: bool = False


async def async_load_bots_config() -> list[BotConfig]:
    """从 MongoDB 加载 Bot 配置列表。"""
    from app.db.repositories.bot_config_repo import BotConfigRepository

    repo = BotConfigRepository()
    docs = await repo.list_all()

    if not docs:
        logger.debug("MongoDB 中无 Bot 配置，等待管理员创建")
        return []

    configs = [
        BotConfig(
            id=d.id,
            name=d.name,
            platform=d.platform,
            app_id=d.app_id,
            app_secret=d.app_secret,
            sandbox_mode=d.sandbox_mode,
            is_blocked=d.is_blocked,
        )
        for d in docs
    ]

    logger.info("从 MongoDB 加载了 %d 个 Bot 配置", len(configs))
    return configs
