"""仓库层测试共用工具：在 mongomock 内存库上下文中运行异步测试体。"""

import asyncio
from typing import Any, Awaitable, Callable

from app.config import settings
from app.db import mongo
from app.models.service import ServiceInDB


def run_with_db(coro_fn: Callable[[], Awaitable[Any]]) -> Any:
    """连接 mongomock 内存库后执行异步测试体，结束后关闭连接。

    每次调用都会新建一个独立的内存库实例，保证测试间数据隔离。
    """

    async def wrapper() -> Any:
        settings.use_mock_db = True
        await mongo.connect_db()
        try:
            return await coro_fn()
        finally:
            await mongo.close_db()

    return asyncio.run(wrapper())


async def ensure_account_service(account_id: str, profile_ref: str) -> ServiceInDB:
    """为账号显式装配首个服务（注册零服务后，仍需要服务的测试用此补齐）。"""
    from app.db.repositories.bot_config_repo import BotConfigRepository
    from app.db.repositories.identity_repo import IdentityRepository
    from app.db.repositories.service_repo import ServiceRepository
    from app.models.service import ServiceCreate

    identities = await IdentityRepository().list_by_account(account_id)
    identity = next((item for item in identities if item.platform is None), identities[0])
    bots = [
        bot
        for bot in await BotConfigRepository().list_by_account(account_id)
        if bot.platform == "web" and not bot.is_blocked
    ]
    bot = bots[0]
    return await ServiceRepository().create(
        ServiceCreate(
            account_id=account_id,
            identity_id=identity.id,
            bot_id=bot.id,
            profile_id=profile_ref,
        )
    )
