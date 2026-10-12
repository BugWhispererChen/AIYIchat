"""仓库层测试共用工具：在 mongomock 内存库上下文中运行异步测试体。"""

import asyncio
from typing import Any, Awaitable, Callable

from app.config import settings
from app.db import mongo


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
