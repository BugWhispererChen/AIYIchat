"""时间上下文中枢（time_context.py）单元测试。

覆盖方案 P1 的时区解析链与降级策略：
- resolve_tz：service → user → 全局默认偏移，逐级回落
- 无效时区名 / tzdata 缺失 → 静默回落固定偏移，不抛异常
- 夏令时（America/New_York）自动处理
- build_context：idle 只认真实对方消息，naive 时间兼容
- render_anchor / render_moment / format_gap 渲染正确性
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.config import settings
from app.conversation.time_context import (
    build_context,
    ensure_aware,
    format_gap,
    render_anchor,
    render_moment,
    resolve_tz,
)

# 固定的“当前时间”：2026-09-02 00:00 UTC == 北京时间 09-02 08:00（星期三）
_NOW_UTC = datetime(2026, 9, 2, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _freeze_now():
    """冻结 time_context 模块内的 datetime.now，使断言与机器时钟无关。"""
    with patch("app.conversation.time_context.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW_UTC
        # 保留真实构造器供内部使用
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        yield mock_dt


@pytest.fixture(autouse=True)
def _default_tz_offset():
    """固定全局默认偏移为 8，避免被其他用例污染。"""
    saved = settings.default_timezone_offset
    settings.default_timezone_offset = 8
    yield
    settings.default_timezone_offset = saved


class TestResolveTz:
    """全系统只读取后台全局 UTC 偏移。"""

    def test_global_offset(self) -> None:
        tz, name = resolve_tz()
        assert name == "UTC+08:00"
        assert tz.utcoffset(None) == timedelta(hours=8)

    def test_offset_change_takes_effect(self) -> None:
        settings.default_timezone_offset = -5
        tz, name = resolve_tz()
        assert name == "UTC-05:00"
        assert tz.utcoffset(None) == timedelta(hours=-5)

    def test_offset_is_clamped(self) -> None:
        settings.default_timezone_offset = 99
        tz, name = resolve_tz()
        assert name == "UTC+14:00"
        assert tz.utcoffset(None) == timedelta(hours=14)


class TestUtcIdle:
    """idle 始终按 UTC 差值计算。"""

    def test_idle_uses_utc_not_local(self) -> None:
        last_msg = datetime(2026, 3, 8, 6, 0, tzinfo=timezone.utc)
        now = datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc)
        with patch("app.conversation.time_context.datetime") as mock_dt:
            mock_dt.now.return_value = now
            ctx = build_context(last_identity_msg_at=last_msg)
        assert ctx.idle_seconds == 3600.0
        assert ctx.idle_phrase == "1 小时"


class TestBuildContext:
    """build_context 的字段完整性与边界。"""

    def test_basic_fields(self) -> None:
        ctx = build_context()
        assert ctx.date_iso == "2026-09-02"
        assert ctx.weekday_cn == "星期三"
        assert ctx.time_hm == "08:00"
        assert ctx.day_phase == "清晨"
        assert ctx.tz_name == "UTC+08:00"
        assert ctx.idle_seconds is None
        assert ctx.idle_phrase == ""

    @pytest.mark.parametrize("hour,expected", [
        (0, "凌晨"), (4, "凌晨"), (5, "清晨"), (8, "清晨"),
        (9, "上午"), (11, "上午"), (12, "中午"), (13, "中午"),
        (14, "下午"), (17, "下午"), (18, "晚上"), (21, "晚上"),
        (22, "深夜"), (23, "深夜"),
    ])
    @pytest.mark.parametrize("minute", [0, 59])
    def test_day_phase_boundaries(
        self, hour: int, minute: int, expected: str
    ) -> None:
        local = datetime(2026, 9, 2, hour, minute, tzinfo=timezone(timedelta(hours=8)))
        with patch("app.conversation.time_context.datetime") as mock_dt:
            mock_dt.now.return_value = local.astimezone(timezone.utc)
            ctx = build_context()
        assert ctx.day_phase == expected
        assert f"【当前时刻】{hour:02d}:{minute:02d}（{expected}）" in render_moment(ctx)

    def test_idle_from_last_identity_message(self) -> None:
        last = _NOW_UTC - timedelta(hours=9)
        ctx = build_context(last_identity_msg_at=last)
        assert ctx.idle_seconds == 9 * 3600
        assert ctx.idle_phrase == "9 小时"

    def test_idle_naive_message_treated_as_utc(self) -> None:
        """存量 naive 消息时间按 UTC 归一，不报错。"""
        naive = datetime(2026, 9, 1, 15, 0)  # 无 tzinfo，按 UTC 处理 → 北京 23:00
        ctx = build_context(last_identity_msg_at=naive)
        assert ctx.idle_seconds == 9 * 3600

    def test_idle_future_message_ignored(self) -> None:
        """未来时间（时钟回拨/异常数据）不产生负 idle。"""
        future = _NOW_UTC + timedelta(hours=1)
        ctx = build_context(last_identity_msg_at=future)
        assert ctx.idle_seconds is None
        assert ctx.idle_phrase == ""

    def test_build_uses_global_offset(self) -> None:
        settings.default_timezone_offset = 9
        ctx = build_context()
        assert ctx.tz_name == "UTC+09:00"


class TestRender:
    """渲染函数的正确性。"""

    def test_render_anchor(self) -> None:
        ctx = build_context()
        text = render_anchor(ctx)
        assert "【时间基准】今天是 2026-09-02（星期三）。" in text
        assert "一律以这个日期为基准推算" in text

    def test_render_moment_with_idle(self) -> None:
        ctx = build_context(last_identity_msg_at=_NOW_UTC - timedelta(days=3, hours=2))
        text = render_moment(ctx)
        assert "【当前时刻】08:00（清晨）" in text
        assert "【对话间隔】距离对方上一条消息已经过去 3 天 2 小时。" in text

    def test_render_moment_without_idle(self) -> None:
        ctx = build_context()
        text = render_moment(ctx)
        assert "【当前时刻】" in text
        assert "【对话间隔】" not in text


class TestFormatGap:
    """间隔中文化纯函数。"""

    @pytest.mark.parametrize("seconds,expected", [
        (0, "不到 1 分钟"),
        (59, "不到 1 分钟"),
        (60, "1 分钟"),
        (20 * 60, "20 分钟"),
        (9 * 3600, "9 小时"),
        (9 * 3600 + 30 * 60, "9 小时 30 分钟"),
        (20 * 86400, "20 天"),
        (3 * 86400 + 2 * 3600, "3 天 2 小时"),
    ])
    def test_format_gap(self, seconds: float, expected: str) -> None:
        assert format_gap(seconds) == expected


class TestEnsureAware:
    """naive → UTC aware 归一。"""

    def test_naive_becomes_utc(self) -> None:
        naive = datetime(2026, 9, 1, 12, 0)
        aware = ensure_aware(naive)
        assert aware.tzinfo is not None
        assert aware.utcoffset() == timezone.utc.utcoffset(None)

    def test_aware_unchanged(self) -> None:
        aware = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        assert ensure_aware(aware) is aware
