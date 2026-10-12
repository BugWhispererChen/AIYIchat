"""主动消息调度器测试（mongomock + mock 管理器/LLM）。

覆盖 ProactiveScheduler 的免打扰判定、通道预检、内容生成、
阶段扫描发送、事件驱动跟进与平台分发。
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import mongomock_motor

from app.config import settings
from app.db import mongo
from app.proactive.scheduler import (
    ProactiveScheduler,
    get_proactive_scheduler,
)
from app.proactive.strategy import QQ_STAGES, get_pending_stage


def _run(coro):
    return asyncio.run(coro)


def _fresh_db():
    mongo._db = mongomock_motor.AsyncMongoMockClient()["test_scheduler"]


class TestStrategy:
    """阶段策略。"""

    def test_get_pending_stage(self) -> None:
        now = datetime.now(timezone.utc)
        # 刚过 4h → 阶段1
        stage = get_pending_stage(now - timedelta(hours=5), [])
        assert stage is not None and stage.stage == 1
        # 阶段1已发 → 阶段2需等 14h，5h 不触发
        assert get_pending_stage(now - timedelta(hours=5), [1]) is None
        # 超过 30 天 → None
        assert get_pending_stage(now - timedelta(days=31), []) is None
        # 未到首个阶段
        assert get_pending_stage(now - timedelta(hours=1), []) is None


class TestSchedulerHelpers:
    """调度器纯函数/预检。"""

    def test_is_uuid(self) -> None:
        assert ProactiveScheduler._is_uuid("12345678-1234-1234-1234-123456789abc")
        assert not ProactiveScheduler._is_uuid("real-openid")

    def test_ensure_aware(self) -> None:
        naive = datetime(2026, 1, 1)
        assert ProactiveScheduler._ensure_aware(naive).tzinfo is not None

    def test_quiet_hours(self) -> None:
        sched = ProactiveScheduler()
        old_start = settings.proactive_quiet_hour_start
        old_end = settings.proactive_quiet_hour_end
        old_tz = settings.proactive_timezone_offset
        try:
            # start == end → 未启用
            settings.proactive_quiet_hour_start = 5
            settings.proactive_quiet_hour_end = 5
            assert sched._is_quiet_hours() is False

            # 跨午夜 22~8：构造当前为凌晨 2 点
            settings.proactive_quiet_hour_start = 22
            settings.proactive_quiet_hour_end = 8
            settings.proactive_timezone_offset = 8
            with patch("app.proactive.scheduler.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 1, 1, 2, 0, tzinfo=timezone.utc)
                assert sched._is_quiet_hours() is True
                mock_dt.now.return_value = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
                assert sched._is_quiet_hours() is False
        finally:
            settings.proactive_quiet_hour_start = old_start
            settings.proactive_quiet_hour_end = old_end
            settings.proactive_timezone_offset = old_tz

    def test_can_send(self) -> None:
        sched = ProactiveScheduler()
        # 管理器未注入 → False
        assert sched._can_send("qq", "b-1", "u-1") is False
        assert sched._can_send("wechat", "b-1", "u-1") is False
        assert sched._can_send("web", "b-1", "u-1") is False

        # QQ 客户端预检
        client = SimpleNamespace(can_send_proactive=lambda uid: True)
        sched._qq_manager = SimpleNamespace(get_client=lambda bid: client)
        assert sched._can_send("qq", "b-1", "u-1") is True
        # 客户端不存在
        sched._qq_manager = SimpleNamespace(get_client=lambda bid: None)
        assert sched._can_send("qq", "b-1", "u-1") is False
        # 客户端无预检方法 → 保守放行
        sched._qq_manager = SimpleNamespace(get_client=lambda bid: SimpleNamespace())
        assert sched._can_send("qq", "b-1", "u-1") is True

    def test_singleton(self) -> None:
        assert get_proactive_scheduler() is get_proactive_scheduler()


class TestSendDispatch:
    """平台分发。"""

    def test_send_message_qq(self) -> None:
        async def body():
            sched = ProactiveScheduler()
            client = SimpleNamespace(send_proactive=AsyncMock(return_value=True))
            sched._qq_manager = SimpleNamespace(get_client=lambda bid: client)
            assert await sched._send_message("qq", "b-1", "u-1", "hi") is True

            # 客户端未找到
            sched._qq_manager = SimpleNamespace(get_client=lambda bid: None)
            assert await sched._send_message("qq", "b-1", "u-1", "hi") is False
            # 管理器未注入
            sched._qq_manager = None
            assert await sched._send_message("qq", "b-1", "u-1", "hi") is False

        _run(body())

    def test_send_message_wechat_and_unknown(self) -> None:
        async def body():
            sched = ProactiveScheduler()
            client = SimpleNamespace(send_proactive=AsyncMock(return_value=True))
            sched._wechat_manager = SimpleNamespace(get_client=lambda bid: client)
            assert await sched._send_message("wechat", "b-1", "u-1", "hi") is True
            sched._wechat_manager = None
            assert await sched._send_message("wechat", "b-1", "u-1", "hi") is False
            # 未知平台
            assert await sched._send_message("web", "b-1", "u-1", "hi") is False

        _run(body())


class TestStartStop:
    """生命周期。"""

    def test_start_stop(self) -> None:
        async def body():
            sched = ProactiveScheduler()
            sched._scan_and_send = AsyncMock()
            await sched.start()
            assert sched._running is True
            # 重复 start 幂等
            await sched.start()
            await asyncio.sleep(0.01)
            await sched.stop()
            assert sched._running is False

        _run(body())


class TestInferEmotion:
    """情绪推断。"""

    def test_infer_recent_emotion(self) -> None:
        async def body():
            _fresh_db()
            try:
                from app.db.repositories.message_repo import MessageRepository

                repo = MessageRepository()
                await repo.create("c-1", "user", "难过", emotion_tag="sad")
                await repo.create("c-1", "user", "还是难过", emotion_tag="sad")
                await repo.create("c-1", "user", "开心", emotion_tag="happy")
                sched = ProactiveScheduler()
                assert await sched._infer_recent_emotion("c-1") == "sad"
                # 无情绪标签
                await repo.create("c-2", "user", "普通")
                assert await sched._infer_recent_emotion("c-2") is None
            finally:
                mongo._db = None

        _run(body())


class TestProcessService:
    """服务处理主流程。"""

    def _make_service(self, **kw):
        base = dict(
            id="svc-1", user_id="u-1", account_id="acc-1",
            bot_id="b-1", profile_id="companion", share_group_id="",
            last_message_at=None,
        )
        base.update(kw)
        ns = SimpleNamespace(**base)
        ns.context_key = ns.share_group_id or ns.id
        return ns

    def _make_user(self, **kw):
        base = dict(
            id="u-1", platform_id="real-openid", platform="qq",
            is_blocked=False, last_active_at=None, identity_description="",
        )
        base.update(kw)
        return SimpleNamespace(**base)

    def test_skip_unbound_and_blocked(self) -> None:
        async def body():
            _fresh_db()
            try:
                sched = ProactiveScheduler()
                svc = self._make_service()
                cutoff = datetime.now(timezone.utc) - timedelta(days=30)

                # 用户不存在
                sched._user_repo.find_by_id = AsyncMock(return_value=None)
                await sched._process_service(svc, cutoff)

                # 被禁用
                sched._user_repo.find_by_id = AsyncMock(
                    return_value=self._make_user(is_blocked=True)
                )
                await sched._process_service(svc, cutoff)

                # 未绑定（platform_id 为 UUID）
                sched._user_repo.find_by_id = AsyncMock(
                    return_value=self._make_user(
                        platform_id="12345678-1234-1234-1234-123456789abc"
                    )
                )
                await sched._process_service(svc, cutoff)

                # 非推送平台
                sched._user_repo.find_by_id = AsyncMock(
                    return_value=self._make_user(platform="web")
                )
                await sched._process_service(svc, cutoff)
            finally:
                mongo._db = None

        _run(body())

    def test_full_send_flow(self) -> None:
        async def body():
            _fresh_db()
            try:
                old_evt = settings.proactive_event_enabled
                old_share = settings.proactive_share_enabled
                settings.proactive_event_enabled = False
                # 关闭分享型随机分支，保证内容生成走被 mock 的关怀路径；
                # 免打扰判定打桩为 False，避免真实时钟（22-8 点）导致用例波动
                settings.proactive_share_enabled = False
                try:
                    sched = ProactiveScheduler()
                    # 用户 5 小时前活跃 → 触发阶段1
                    last = datetime.now(timezone.utc) - timedelta(hours=5)
                    user = self._make_user(last_active_at=last)
                    svc = self._make_service()

                    sched._user_repo.find_by_id = AsyncMock(return_value=user)
                    sched._can_send = lambda *a, **k: True
                    sched._is_quiet_hours = lambda: False
                    sched._generate_content = AsyncMock(return_value=("你好呀", ""))
                    sched._send_message = AsyncMock(return_value=True)

                    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
                    await sched._process_service(svc, cutoff)
                    sched._send_message.assert_awaited()
                finally:
                    settings.proactive_event_enabled = old_evt
                    settings.proactive_share_enabled = old_share
            finally:
                mongo._db = None

        _run(body())

    def test_skip_recently_active(self) -> None:
        async def body():
            _fresh_db()
            try:
                sched = ProactiveScheduler()
                # 1 小时前活跃 → 跳过（<4h）
                user = self._make_user(
                    last_active_at=datetime.now(timezone.utc) - timedelta(hours=1)
                )
                sched._user_repo.find_by_id = AsyncMock(return_value=user)
                sched._send_message = AsyncMock()
                cutoff = datetime.now(timezone.utc) - timedelta(days=30)
                await sched._process_service(self._make_service(), cutoff)
                sched._send_message.assert_not_called()
            finally:
                mongo._db = None

        _run(body())


class TestGenerateContent:
    """内容生成。"""

    def test_generate_content(self) -> None:
        async def body():
            _fresh_db()
            try:
                from app.db.repositories.bot_config_repo import BotConfigRepository
                from app.db.repositories.profile_repo import ProfileRepository
                from app.models.bot_config import BotConfigCreate
                from app.models.profile import PromptProfileCreate

                bot = await BotConfigRepository().create(
                    BotConfigCreate(account_id="acc-1", name="Bot")
                )
                await ProfileRepository().create(
                    PromptProfileCreate(slug="companion", name="小伴")
                )

                sched = ProactiveScheduler()
                svc = SimpleNamespace(
                    id="svc-1", bot_id=bot.id, profile_id="companion",
                    share_group_id="", context_key="svc-1",
                )
                user = SimpleNamespace(
                    id="u-1", identity_description="", platform="qq",
                )
                fake_provider = SimpleNamespace(
                    complete=AsyncMock(return_value="想你了，最近怎么样？")
                )
                stage = QQ_STAGES[0]
                with patch(
                    "app.llm.provider_factory.get_llm_provider",
                    return_value=fake_provider,
                ):
                    content, ref = await sched._generate_content(user, svc, stage)
                assert "想你了" in content

                # bot 不存在 → 空
                svc2 = SimpleNamespace(
                    id="svc-2", bot_id="no-bot", profile_id="companion",
                    share_group_id="", context_key="svc-2",
                )
                content2, _ = await sched._generate_content(user, svc2, stage)
                assert content2 == ""
            finally:
                mongo._db = None

        _run(body())


class TestPendingEvents:
    """事件驱动跟进。"""

    def test_process_pending_events(self) -> None:
        async def body():
            _fresh_db()
            try:
                from app.db.repositories.service_memory_repo import (
                    ServiceMemoryRepository,
                )
                from app.models.service_memory import PendingEvent

                mem_repo = ServiceMemoryRepository()
                due = datetime.now(timezone.utc) - timedelta(hours=1)
                await mem_repo.add_pending_events("svc-1", [
                    PendingEvent(content="面试结果", due_at=due),
                ])

                sched = ProactiveScheduler()
                svc = SimpleNamespace(
                    id="svc-1", bot_id="b-1", profile_id="companion",
                    share_group_id="", context_key="svc-1",
                )
                user = SimpleNamespace(
                    id="u-1", platform="qq", platform_id="oid-1",
                )
                sched._can_send = lambda *a, **k: True
                sched._is_quiet_hours = lambda: False
                sched._generate_event_content = AsyncMock(return_value="面试怎么样啦？")
                sched._send_message = AsyncMock(return_value=True)

                handled = await sched._process_pending_events(svc, user)
                assert handled is True
                sched._send_message.assert_awaited()

                # 事件已跟进后不再处理
                handled2 = await sched._process_pending_events(svc, user)
                assert handled2 is False
            finally:
                mongo._db = None

        _run(body())

    def test_no_pending_events(self) -> None:
        async def body():
            _fresh_db()
            try:
                sched = ProactiveScheduler()
                svc = SimpleNamespace(
                    id="svc-x", context_key="svc-x",
                )
                user = SimpleNamespace(id="u-1", platform="qq", platform_id="oid")
                assert await sched._process_pending_events(svc, user) is False
            finally:
                mongo._db = None

        _run(body())


class TestScanAndSend:
    """整轮扫描。"""

    def test_scan_disabled(self) -> None:
        async def body():
            sched = ProactiveScheduler()
            old = settings.proactive_enabled
            settings.proactive_enabled = False
            try:
                # 关闭时直接返回，不触碰 DB
                await sched._scan_and_send()
            finally:
                settings.proactive_enabled = old

        _run(body())

    def test_scan_processes_services(self) -> None:
        async def body():
            sched = ProactiveScheduler()
            old = settings.proactive_enabled
            settings.proactive_enabled = True
            try:
                svc = SimpleNamespace(id="svc-1")
                sched._service_repo.list_all_active = AsyncMock(return_value=[svc])
                sched._process_service = AsyncMock()
                await sched._scan_and_send()
                sched._process_service.assert_awaited()

                # 无活跃服务
                sched._service_repo.list_all_active = AsyncMock(return_value=[])
                await sched._scan_and_send()
            finally:
                settings.proactive_enabled = old

        _run(body())


class TestMultiBotRouting:
    """多 Bot 场景：按 Bot 路由 openid、同 Bot 冷却、失败详情留痕。"""

    def _make_service(self, **kw):
        base = dict(
            id="svc-1", user_id="u-1", account_id="acc-1",
            bot_id="b-1", profile_id="companion", share_group_id="",
            last_message_at=None,
        )
        base.update(kw)
        ns = SimpleNamespace(**base)
        ns.context_key = ns.share_group_id or ns.id
        return ns

    def _make_user(self, **kw):
        base = dict(
            id="u-1", platform_id="oid-latest", platform="qq",
            is_blocked=False, identity_description="",
            last_active_at=datetime.now(timezone.utc) - timedelta(hours=5),
            bot_openids={},
        )
        base.update(kw)
        return SimpleNamespace(**base)

    def test_resolve_platform_id(self) -> None:
        """优先用该 Bot 的 openid，缺失时回退 platform_id。"""
        sched = ProactiveScheduler()
        user = self._make_user(bot_openids={"b-1": "oid-b1", "b-2": "oid-b2"})
        assert sched._resolve_platform_id(user, "b-1") == "oid-b1"
        assert sched._resolve_platform_id(user, "b-2") == "oid-b2"
        # 无映射的 Bot（存量数据）→ 回退
        assert sched._resolve_platform_id(user, "b-3") == "oid-latest"
        # 无 bot_openids 属性的旧对象 → 回退
        legacy = SimpleNamespace(id="u-1", platform_id="oid-latest")
        assert sched._resolve_platform_id(legacy, "b-1") == "oid-latest"

    def test_each_bot_service_sends_own_openid(self) -> None:
        """同一用户的不同 Bot 服务各自触达，且发到各自的 openid。"""
        async def body():
            _fresh_db()
            old_evt = settings.proactive_event_enabled
            old_share = settings.proactive_share_enabled
            settings.proactive_event_enabled = False
            settings.proactive_share_enabled = False
            try:
                sched = ProactiveScheduler()
                user = self._make_user(
                    bot_openids={"b-1": "oid-b1", "b-2": "oid-b2"}
                )
                sched._user_repo.find_by_id = AsyncMock(return_value=user)
                sched._can_send = lambda *a, **k: True
                sched._is_quiet_hours = lambda: False
                sched._generate_content = AsyncMock(return_value=("你好呀", ""))
                sched._send_message = AsyncMock(return_value=True)

                cutoff = datetime.now(timezone.utc) - timedelta(days=30)
                await sched._process_service(self._make_service(), cutoff)
                await sched._process_service(
                    self._make_service(id="svc-2", bot_id="b-2"), cutoff
                )

                assert sched._send_message.await_count == 2
                targets = {
                    c.kwargs["user_platform_id"]
                    for c in sched._send_message.await_args_list
                }
                assert targets == {"oid-b1", "oid-b2"}
            finally:
                settings.proactive_event_enabled = old_evt
                settings.proactive_share_enabled = old_share
                mongo._db = None

        _run(body())

    def test_same_bot_cooldown_skips(self) -> None:
        """同 Bot 一小时内已发过 → 本轮跳过（防多服务秒级连发）。"""
        async def body():
            _fresh_db()
            try:
                from app.db.repositories.proactive_repo import (
                    ProactiveRecordRepository,
                )
                from app.models.proactive_record import ProactiveRecord

                repo = ProactiveRecordRepository()
                sent = ProactiveRecord(
                    user_id="u-1", platform_user_id="oid-b1", platform="qq",
                    bot_id="b-1", stage=1,
                )
                await repo.create(sent)
                await repo.mark_sent(sent.id, "刚发过的一条")

                sched = ProactiveScheduler()
                sched._user_repo.find_by_id = AsyncMock(
                    return_value=self._make_user()
                )
                sched._send_message = AsyncMock()
                cutoff = datetime.now(timezone.utc) - timedelta(days=30)
                await sched._process_service(self._make_service(), cutoff)
                sched._send_message.assert_not_called()
            finally:
                mongo._db = None

        _run(body())

    def test_send_failure_records_platform_error(self) -> None:
        """发送失败时记录 QQ 真实错误详情，而非笼统文案。"""
        async def body():
            _fresh_db()
            old_evt = settings.proactive_event_enabled
            old_share = settings.proactive_share_enabled
            settings.proactive_event_enabled = False
            settings.proactive_share_enabled = False
            try:
                from app.db.repositories.proactive_repo import (
                    ProactiveRecordRepository,
                )

                sched = ProactiveScheduler()
                sched._user_repo.find_by_id = AsyncMock(
                    return_value=self._make_user(bot_openids={"b-1": "oid-b1"})
                )
                sched._can_send = lambda *a, **k: True
                sched._is_quiet_hours = lambda: False
                sched._generate_content = AsyncMock(return_value=("在吗", ""))
                client = SimpleNamespace(
                    send_proactive=AsyncMock(return_value=False),
                    last_send_error="QQ错误 40034122: 召回消息已达区间上限",
                )
                sched._qq_manager = SimpleNamespace(
                    get_client=lambda bid: client
                )

                cutoff = datetime.now(timezone.utc) - timedelta(days=30)
                await sched._process_service(self._make_service(), cutoff)

                records = await ProactiveRecordRepository().find_latest_by_user(
                    "u-1", "b-1", 1
                )
                assert records and records[0].status == "failed"
                assert "40034122" in records[0].error_msg
                assert records[0].platform_user_id == "oid-b1"

                # 客户端无错误详情属性 → 兜底文案
                sched._qq_manager = SimpleNamespace(
                    get_client=lambda bid: SimpleNamespace(
                        send_proactive=AsyncMock(return_value=False)
                    )
                )
                await sched._process_service(
                    self._make_service(id="svc-9", bot_id="b-9"), cutoff
                )
                assert sched._last_send_detail == ""
            finally:
                settings.proactive_event_enabled = old_evt
                settings.proactive_share_enabled = old_share
                mongo._db = None

        _run(body())
