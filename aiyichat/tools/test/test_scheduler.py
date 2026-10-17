"""主动消息调度器测试（mongomock + mock 管理器/LLM）。

覆盖 ProactiveScheduler 的免打扰判定、通道预检、内容生成、
阶段扫描发送、事件驱动跟进与平台分发。
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import mongomock_motor
import pytest

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

    @pytest.mark.parametrize("hour,expected", [
        (0, "凌晨"), (4, "凌晨"), (5, "清晨"), (8, "清晨"),
        (9, "上午"), (11, "上午"), (12, "中午"), (13, "中午"),
        (14, "下午"), (17, "下午"), (18, "晚上"), (21, "晚上"),
        (22, "深夜"), (23, "深夜"),
    ])
    @pytest.mark.parametrize("minute", [0, 59])
    def test_time_hint_boundaries(
        self, hour: int, minute: int, expected: str
    ) -> None:
        local = datetime(2026, 9, 2, hour, minute, tzinfo=timezone(timedelta(hours=8)))
        hints = {
            "凌晨": "可以温柔提醒对方早点休息",
            "清晨": "可以问候早安、关心对方是否睡得好、今天有什么计划",
            "上午": "可以关心对方上午的工作/学习状态",
            "中午": "可以关心对方吃午饭了吗、午休情况",
            "下午": "可以关心对方下午的工作进展、是否累了需要休息",
            "晚上": "可以关心对方晚饭、今天的收获、放松休息",
            "深夜": "可以温柔提醒对方早点休息",
        }
        with patch("app.proactive.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = local
            assert ProactiveScheduler()._time_hint() == (local, expected, hints[expected])

    def test_quiet_hours(self) -> None:
        sched = ProactiveScheduler()
        old_start = settings.proactive_quiet_hour_start
        old_end = settings.proactive_quiet_hour_end
        old_tz = settings.default_timezone_offset
        try:
            # start == end → 未启用
            settings.proactive_quiet_hour_start = 5
            settings.proactive_quiet_hour_end = 5
            assert sched._is_quiet_hours() is False

            # 跨午夜 22~8：构造当前为凌晨 2 点
            settings.proactive_quiet_hour_start = 22
            settings.proactive_quiet_hour_end = 8
            settings.default_timezone_offset = 8
            with patch("app.proactive.scheduler.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 1, 1, 2, 0, tzinfo=timezone.utc)
                assert sched._is_quiet_hours() is True
                mock_dt.now.return_value = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
                assert sched._is_quiet_hours() is False
        finally:
            settings.proactive_quiet_hour_start = old_start
            settings.proactive_quiet_hour_end = old_end
            settings.default_timezone_offset = old_tz

    def test_quiet_hours_falls_back_to_global_offset(self) -> None:
        """未配置对方时区时回落到全局偏移（保持原行为，不 regress）。"""
        sched = ProactiveScheduler()
        old_tz = settings.default_timezone_offset
        try:
            settings.default_timezone_offset = 8
            with patch("app.proactive.scheduler.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
                sched._is_quiet_hours(SimpleNamespace(timezone=""), SimpleNamespace(timezone=""))
                assert mock_dt.now.call_args[0][0] == timezone(timedelta(hours=8))
        finally:
            settings.default_timezone_offset = old_tz

    def test_quiet_hours_uses_service_timezone(self) -> None:
        """免打扰按 service.timezone 取当地小时，而非服务器全局偏移。

        动机：按全局偏移判"深夜别打扰"，会在海外用户当地凌晨发消息，
        既扰人，也暴露角色不关心对方身在何处。
        """
        from zoneinfo import ZoneInfo

        try:
            expected = ZoneInfo("America/Los_Angeles")
        except Exception:
            pytest.skip("本机缺少 tzdata，跳过 IANA 时区用例")
        sched = ProactiveScheduler()
        old_tz = settings.default_timezone_offset
        try:
            settings.default_timezone_offset = 8
            with patch("app.proactive.scheduler.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
                sched._is_quiet_hours(
                    SimpleNamespace(timezone="America/Los_Angeles"),
                    SimpleNamespace(timezone=""),
                )
                assert mock_dt.now.call_args[0][0] is expected
        finally:
            settings.default_timezone_offset = old_tz

    def test_time_hint_prefers_user_timezone_when_service_empty(self) -> None:
        """service 未设时区时按解析链回落到 user.timezone。"""
        from zoneinfo import ZoneInfo

        try:
            expected = ZoneInfo("Asia/Tokyo")
        except Exception:
            pytest.skip("本机缺少 tzdata，跳过 IANA 时区用例")
        sched = ProactiveScheduler()
        with patch("app.proactive.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
            sched._time_hint(
                SimpleNamespace(timezone=""),
                SimpleNamespace(timezone="Asia/Tokyo"),
            )
            assert mock_dt.now.call_args[0][0] is expected

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
                    sched._is_quiet_hours = lambda *a, **k: False
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

    @pytest.mark.parametrize("kind", ["care", "news", "trivia", "event"])
    @pytest.mark.parametrize("raw_response,expected", [
        (" [10-16 02:00] 想你了，最近怎么样？ ", "想你了，最近怎么样？"),
        (
            "\t[2026-10-16 02:00] 最近怎么样？\n[10-16 02:01] 记得休息。",
            "最近怎么样？\n记得休息。",
        ),
        ("[10-16 02:00] " + "好" * 110, "好" * 100),
        ("今天 [10-16 02:00] 记录的提醒", "今天 [10-16 02:00] 记录的提醒"),
        ("  晚安  ", "晚安"),
        ("[10-16 02:00]", ""),
    ])
    def test_generate_content(
        self, kind: str, raw_response: str, expected: str
    ) -> None:
        async def body() -> None:
            _fresh_db()
            try:
                from app.db.repositories.bot_config_repo import BotConfigRepository
                from app.db.repositories.conversation_repo import ConversationRepository
                from app.db.repositories.profile_repo import ProfileRepository
                from app.db.repositories.service_memory_repo import ServiceMemoryRepository
                from app.llm.prompt_assembler import PromptAssembler
                from app.models.bot_config import BotConfigCreate
                from app.models.message import MessageInDB
                from app.models.profile import PromptProfileCreate
                from app.models.service_memory import MemoryFact

                bot = await BotConfigRepository().create(
                    BotConfigCreate(account_id="acc-1", name="Bot")
                )
                await ProfileRepository().create(
                    PromptProfileCreate(slug="companion", name="小伴")
                )
                conversation = await ConversationRepository().create(
                    "u-1", "companion", "svc-1"
                )
                history = [MessageInDB(
                    _id="msg-1", conversation_id=conversation.id, role="user",
                    content="上次和朋友完成了登山",
                    created_at=datetime.now(timezone.utc) - timedelta(hours=5),
                )]
                memory_repo = ServiceMemoryRepository()
                await memory_repo.upsert_topic("svc-1", "旧话题：爬山路线")
                await memory_repo.add_facts("svc-1", [MemoryFact(content="喜欢登山")])

                sched = ProactiveScheduler()
                svc = SimpleNamespace(
                    id="svc-1", bot_id=bot.id, profile_id="companion",
                    share_group_id="", context_key="svc-1",
                )
                user = SimpleNamespace(
                    id="u-1", identity_description="", platform="qq",
                )
                fake_provider = SimpleNamespace(
                    complete=AsyncMock(side_effect=[
                        raw_response, "[2026-10-16 02:01] 分享个有趣的小发现。",
                    ])
                )
                stage = QQ_STAGES[0]
                with (
                    patch("app.llm.provider_factory.get_llm_provider",
                          return_value=fake_provider),
                    patch("app.conversation.context_manager.ContextManager.get_context",
                          new=AsyncMock(return_value=(history, None))),
                    patch("app.llm.prompt_assembler.PromptAssembler.assemble",
                          wraps=PromptAssembler().assemble) as assemble,
                    patch.object(settings, "message_time_enabled", True),
                ):
                    if kind == "care":
                        content, ref = await sched._generate_content(user, svc, stage)
                        assert ref == ""
                    elif kind == "event":
                        content = await sched._generate_event_content(user, svc, "面试结果")
                    else:
                        content = await sched._generate_share_content(user, svc, kind)

                fallback = kind == "news" and not expected
                assert content == ("分享个有趣的小发现。" if fallback else expected)
                assert fake_provider.complete.await_count == (2 if fallback else 1)
                assert assemble.call_count == fake_provider.complete.await_count
                for call in assemble.call_args_list:
                    assert call.kwargs["gap_mode"] == "proactive"
                    assert call.kwargs["current_topic"] == ""
                    assert call.kwargs.get("topic_context", "") == ""
                    assert call.kwargs["history"] == history
                    if kind != "event":
                        assert call.kwargs["memory_facts"] == ["喜欢登山"]
                for call in fake_provider.complete.await_args_list:
                    prompt = "\n".join(message["content"] for message in call.args[0])
                    assert "【主动开口】" in prompt
                    assert "正在聊" not in prompt
                    assert "→当前" not in prompt and "→ 当前" not in prompt
                    assert "旧话题：爬山路线" not in prompt
                    assert "上次和朋友完成了登山" in prompt
                    if kind != "event":
                        assert "喜欢登山" in prompt
                    else:
                        assert "面试结果" in prompt
                if kind in ("news", "trivia"):
                    calls = fake_provider.complete.await_args_list
                    assert calls[0].kwargs["web_search"] == (kind == "news")
                    if fallback:
                        assert calls[1].kwargs["web_search"] is False
                memory = await memory_repo.find_by_service("svc-1")
                assert memory is not None
                assert memory.current_topic == "旧话题：爬山路线"
                assert [fact.content for fact in memory.facts] == ["喜欢登山"]

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
                sched._is_quiet_hours = lambda *a, **k: False
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
                sched._is_quiet_hours = lambda *a, **k: False
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
                sched._is_quiet_hours = lambda *a, **k: False
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
