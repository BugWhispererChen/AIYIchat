"""主动关怀调度器单元测试。

覆盖 ProactiveScheduler 的纯逻辑部分（不连数据库/LLM）：
- _is_quiet_hours：配置驱动的免打扰时段判定（跨午夜/同日/未启用）
- _can_send：发送通道预检（管理器缺失/客户端缺失/预检方法缺失或拒绝）
- strategy.get_pending_stage：阶段触发判定（延迟/已发送/超期）
- _process_service：非推送平台（web）身份的主动关怀跳过守卫
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import settings
from app.proactive.scheduler import ProactiveScheduler
from app.proactive.strategy import get_pending_stage


def _fixed_hour(scheduler: ProactiveScheduler, hour: int) -> bool:
    """在固定的本地小时下调用 _is_quiet_hours。"""
    fake_now = datetime(2026, 1, 1, hour, 0, tzinfo=timezone.utc)
    with patch("app.proactive.scheduler.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        return scheduler._is_quiet_hours()


class TestIsQuietHours:
    """免打扰时段判定测试集（默认配置 22~8，UTC+8）。"""

    def setup_method(self) -> None:
        self.scheduler = ProactiveScheduler()

    def _set(self, monkeypatch, start: int, end: int) -> None:
        monkeypatch.setattr(settings, "proactive_quiet_hour_start", start)
        monkeypatch.setattr(settings, "proactive_quiet_hour_end", end)
        monkeypatch.setattr(settings, "default_timezone_offset", 8)

    def test_late_night_is_quiet(self, monkeypatch) -> None:
        """跨午夜场景：23 点处于免打扰。"""
        self._set(monkeypatch, 22, 8)
        assert _fixed_hour(self.scheduler, 23) is True

    def test_early_morning_is_quiet(self, monkeypatch) -> None:
        """跨午夜场景：早上 7 点仍处于免打扰。"""
        self._set(monkeypatch, 22, 8)
        assert _fixed_hour(self.scheduler, 7) is True

    def test_daytime_not_quiet(self, monkeypatch) -> None:
        """白天 12 点不处于免打扰。"""
        self._set(monkeypatch, 22, 8)
        assert _fixed_hour(self.scheduler, 12) is False

    def test_boundary_end_hour_not_quiet(self, monkeypatch) -> None:
        """结束时刻整点（8 点）恢复发送。"""
        self._set(monkeypatch, 22, 8)
        assert _fixed_hour(self.scheduler, 8) is False

    def test_same_day_window(self, monkeypatch) -> None:
        """同日场景（1~6 点）：3 点免打扰，7 点不免打扰。"""
        self._set(monkeypatch, 1, 6)
        assert _fixed_hour(self.scheduler, 3) is True
        assert _fixed_hour(self.scheduler, 7) is False

    def test_disabled_when_start_equals_end(self, monkeypatch) -> None:
        """start == end 视为未启用免打扰。"""
        self._set(monkeypatch, 0, 0)
        assert _fixed_hour(self.scheduler, 3) is False


class TestCanSend:
    """发送通道预检测试集（防止无效通道消耗 LLM 额度）。"""

    def setup_method(self) -> None:
        self.scheduler = ProactiveScheduler()

    def test_qq_manager_missing_blocks(self) -> None:
        """QQ 管理器未注入 → 不可发送。"""
        assert self.scheduler._can_send("qq", "bot1", "u1") is False

    def test_client_missing_blocks(self) -> None:
        """客户端未找到 → 不可发送。"""
        manager = MagicMock()
        manager.get_client.return_value = None
        self.scheduler._qq_manager = manager
        assert self.scheduler._can_send("qq", "bot1", "u1") is False

    def test_client_without_checker_allows(self) -> None:
        """客户端未实现预检方法 → 保守放行，交由实际发送环节处理。"""
        client = MagicMock(spec=[])  # 无 can_send_proactive 属性
        manager = MagicMock()
        manager.get_client.return_value = client
        self.scheduler._qq_manager = manager
        assert self.scheduler._can_send("qq", "bot1", "u1") is True

    def test_checker_rejects_blocks(self) -> None:
        """预检方法返回 False → 不可发送（如 QQ 限流退避期）。"""
        client = MagicMock()
        client.can_send_proactive.return_value = False
        manager = MagicMock()
        manager.get_client.return_value = client
        self.scheduler._wechat_manager = manager
        assert self.scheduler._can_send("wechat", "bot1", "u1") is False

    def test_unknown_platform_blocks(self) -> None:
        """未知平台 → 不可发送。"""
        assert self.scheduler._can_send("web", "bot1", "u1") is False


class TestGetPendingStage:
    """阶段触发判定测试集（4h/24h/72h/168h 渐进召回）。"""

    @staticmethod
    def _ago(hours: float) -> datetime:
        return datetime.now(timezone.utc) - timedelta(hours=hours)

    def test_too_recent_no_stage(self) -> None:
        """最后活跃 1 小时内 → 无待发送阶段。"""
        assert get_pending_stage(self._ago(1), []) is None

    def test_stage1_after_4_hours(self) -> None:
        """超过 4 小时 → 阶段 1。"""
        stage = get_pending_stage(self._ago(5), [])
        assert stage is not None
        assert stage.stage == 1

    def test_stage1_sent_waits_for_stage2(self) -> None:
        """阶段 1 已发送、未到 24h → 无待发送阶段。"""
        assert get_pending_stage(self._ago(5), [1]) is None

    def test_stage3_after_72_hours(self) -> None:
        """阶段 1/2 已发送、超过 72h → 阶段 3。"""
        stage = get_pending_stage(self._ago(80), [1, 2])
        assert stage is not None
        assert stage.stage == 3

    def test_stage2_between_24_and_72_hours(self) -> None:
        """阶段 1 已发送、超过 24h 未到 72h → 阶段 2。"""
        stage = get_pending_stage(self._ago(30), [1])
        assert stage is not None
        assert stage.stage == 2

    def test_failed_attempts_count_as_sent(self) -> None:
        """已尝试（含失败达上限）的阶段不再触发，防止额度耗尽。"""
        assert get_pending_stage(self._ago(50), [1, 2, 3, 4]) is None

    def test_over_30_days_no_stage(self) -> None:
        """超过 30 天（720h）不再发送。"""
        assert get_pending_stage(self._ago(721), []) is None


class TestResolvePlatformId:
    """发送目标解析测试集（按 Bot 路由 openid，避免多 appid 错配）。"""

    def setup_method(self) -> None:
        self.scheduler = ProactiveScheduler()

    @staticmethod
    def _identity(**overrides):
        base = dict(id="u1", platform_id="oid-latest", bot_openids={})
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_uses_bot_specific_openid(self) -> None:
        """存在该 Bot 的映射时优先使用，不受 platform_id 覆盖影响。"""
        identity = self._identity(bot_openids={"bot-a": "oid-a", "bot-b": "oid-b"})
        assert self.scheduler._resolve_platform_id(identity, "bot-a") == "oid-a"
        assert self.scheduler._resolve_platform_id(identity, "bot-b") == "oid-b"

    def test_falls_back_to_platform_id(self) -> None:
        """无该 Bot 映射（存量数据）→ 回退 platform_id。"""
        identity = self._identity(bot_openids={"bot-a": "oid-a"})
        assert self.scheduler._resolve_platform_id(identity, "bot-z") == "oid-latest"

    def test_legacy_identity_without_mapping_field(self) -> None:
        """旧对象无 bot_openids 属性 → 回退 platform_id 且不报错。"""
        legacy = SimpleNamespace(id="u1", platform_id="oid-latest")
        assert self.scheduler._resolve_platform_id(legacy, "bot-a") == "oid-latest"

    def test_group_openid_mapping_preserved(self) -> None:
        """群聊联合键（group_id::member_openid）原样保存与返回。"""
        identity = self._identity(bot_openids={"bot-a": "gid::mid"})
        assert self.scheduler._resolve_platform_id(identity, "bot-a") == "gid::mid"


class TestWebPrincipalSkipped:
    """非推送平台守卫测试集（_process_service 的平台白名单早退）。"""

    def setup_method(self) -> None:
        self.scheduler = ProactiveScheduler()

    def test_web_user_never_receives_proactive_message(self, monkeypatch) -> None:
        """网页身份由 C 端会话被动应答，主动关怀必须跳过（防止向无真实平台通道的身份发送）。

        回归动机：注册身份以 User(platform="web", platform_id=<用户名>) 入库，
        其 platform_id 是小写用户名（非 UUID、非 openid），不会被前置的
        UUID 未绑定检查拦截——唯一防线是 _process_service 中的平台白名单守卫。
        本用例将守卫之后的整条"合格服务"链路全部打桩为可发送，若守卫被重构
        移除，发送与建档断言会立即失败，锁定该行为不被静默破坏。
        """
        # 事件驱动/分享型分支关闭，保证判定路径收敛到阶段策略主干
        monkeypatch.setattr(settings, "proactive_event_enabled", False)
        monkeypatch.setattr(settings, "proactive_share_enabled", False)

        identity = SimpleNamespace(
            id="u-web-1",
            platform="web",
            platform_id="alice",  # 用户名而非 openid，刻意避开 UUID 兜底检查
            is_blocked=False,
            last_active_at=datetime.now(timezone.utc) - timedelta(hours=5),
            timezone="",
            bot_openids={},
        )
        service = SimpleNamespace(
            id="svc-web-1", identity_id="u-web-1", account_id="acc-1",
            bot_id="b-1", profile_id="companion", share_group_id="",
            last_message_at=None, timezone="",
        )
        service.context_key = service.share_group_id or service.id

        # 守卫之后的链路全部打桩为"本可发送"，仅靠守卫本身拦截
        self.scheduler._identity_repo.find_by_id = AsyncMock(return_value=identity)
        self.scheduler._proactive_repo.get_latest_sent = AsyncMock(return_value=None)
        self.scheduler._proactive_repo.get_attempted_stages = AsyncMock(return_value=[])
        self.scheduler._proactive_repo.count_sent_this_month = AsyncMock(return_value=0)
        self.scheduler._proactive_repo.create = AsyncMock()
        self.scheduler._generate_content = AsyncMock(return_value=("你好呀", ""))
        self.scheduler._send_message = AsyncMock(return_value=True)
        self.scheduler._can_send = lambda *a, **k: True
        self.scheduler._is_quiet_hours = lambda *a, **k: False

        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        asyncio.run(self.scheduler._process_service(service, cutoff))

        # 未产生主动消息记录、未生成内容、未尝试发送
        self.scheduler._proactive_repo.create.assert_not_awaited()
        self.scheduler._generate_content.assert_not_awaited()
        self.scheduler._send_message.assert_not_awaited()
        # 守卫早退：连发送前置的冷却查询都不应发生
        self.scheduler._proactive_repo.get_latest_sent.assert_not_awaited()
