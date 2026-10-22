"""虚拟时钟重放助手：让"按小时/天演进"的确定性状态机能在离线场景里快进验证。

主动性退让（C10）、作息（C1）这类机制的真实节奏是 4h/24h/72h/168h，
靠线上聊几句既等不起也复现不了；本模块把它们改成可推进的虚拟时钟。

**没有引入 freezegun/time-machine**：它们会连 mongomock、tzdata 与其它第三方库的取时
一起冻住，且与本仓既有的「逐模块 `mock.patch("<模块>.datetime")`」约定并存时极易假绿
（表现为"时间被冻住了但某处仍在用真时钟"）。这里沿用同一约定，只替换指定模块里的
`datetime` 名字，其余构造、比较、`min/timedelta` 全部走真实实现。
"""

import importlib
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

# 默认覆盖：调度器判定 + 阶段触发 + 记录写入时间 + 记录模型的 created_at 默认工厂
# （四者必须共用同一个 now，否则"发送时间"与"判定时间"来自两个时钟，重放结论直接失真）
DEFAULT_MODULES = (
    "app.proactive.scheduler",
    "app.proactive.strategy",
    "app.db.repositories.proactive_repo",
    "app.db.repositories.message_repo",
    "app.models.proactive_record",
)


def _make_frozen_datetime(start: datetime) -> type[datetime]:
    """构造一个只替换 now() 的 datetime 子类（其余行为继承真实 datetime）。"""
    clock = start if start.tzinfo else start.replace(tzinfo=timezone.utc)

    class _FrozenDateTime(datetime):
        now_value = clock

        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            if tz is None:
                return cls.now_value
            return cls.now_value.astimezone(tz)

    return _FrozenDateTime


class ReplayClock:
    """可推进的虚拟时钟。"""

    def __init__(self, start: datetime) -> None:
        self._fake = _make_frozen_datetime(start)

    @property
    def now(self) -> datetime:
        """当前虚拟时间（UTC aware）。"""
        return self._fake.now_value

    def advance(self, *, hours: float = 0, minutes: float = 0) -> datetime:
        """按小时/分钟推进虚拟时钟并返回新的当前时间。"""
        delta = timedelta(hours=hours, minutes=minutes)
        self._fake.now_value = self._fake.now_value + delta
        return self._fake.now_value

    def jump_to(self, when: datetime) -> datetime:
        """直接把时钟拨到指定时刻（naive 按 UTC 处理）。"""
        self._fake.now_value = (
            when if when.tzinfo else when.replace(tzinfo=timezone.utc)
        )
        return self._fake.now_value


@contextmanager
def frozen_clock(
    start: datetime,
    modules: tuple[str, ...] = DEFAULT_MODULES,
) -> Iterator[ReplayClock]:
    """在指定模块内把 `datetime.now()` 冻结为可推进的虚拟时钟。

    用法：
        with frozen_clock(datetime(2026, 10, 1, 10, tzinfo=timezone.utc)) as clock:
            await scheduler._process_service(service, cutoff)
            clock.advance(hours=24)
            ...
    """
    clock = ReplayClock(start)
    with ExitStack() as stack:
        for name in modules:
            module = importlib.import_module(name)
            stack.enter_context(patch.object(module, "datetime", clock._fake))
        yield clock
