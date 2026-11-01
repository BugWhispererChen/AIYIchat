"""会话级串行：同一 (bot, platform, 对方) 的整轮处理不能并发重叠。"""

import asyncio
from typing import Any

from app.conversation.engine import ConversationEngine


def test_same_session_messages_are_serialized() -> None:
    engine = ConversationEngine(bot_id="bot-lock")
    events: list[tuple[str, str]] = []

    async def fake_round(**kwargs: Any) -> str:
        events.append(("start", kwargs["text"]))
        await asyncio.sleep(0.02)
        events.append(("end", kwargs["text"]))
        return "ok"

    engine._handle_message_locked = fake_round  # type: ignore[method-assign]

    async def case() -> None:
        await asyncio.gather(
            engine.handle_message("qq", "openid-1", "first"),
            engine.handle_message("qq", "openid-1", "second"),
        )

    asyncio.run(case())
    assert events == [
        ("start", "first"),
        ("end", "first"),
        ("start", "second"),
        ("end", "second"),
    ], "同一会话的两轮处理发生了并发重叠或乱序"


def test_different_sessions_can_overlap() -> None:
    engine = ConversationEngine(bot_id="bot-lock-2")
    starts = 0

    async def fake_round(**kwargs: Any) -> str:
        nonlocal starts
        starts += 1
        await asyncio.sleep(0.02)
        return "ok"

    engine._handle_message_locked = fake_round  # type: ignore[method-assign]

    async def case() -> None:
        await asyncio.gather(
            engine.handle_message("qq", "openid-a", "a"),
            engine.handle_message("qq", "openid-b", "b"),
        )

    asyncio.run(case())
    assert starts == 2
