"""ConversationEngine.handle_message 核心编排路径行为测试。

用 mock 隔离数据库/网络（模式同 test_bugfixes.py），聚焦主链路不变量：
- 正常路径：Bot 校验→身份/服务解析→额度预检→prompt 组装→LLM→usage 结算→持久化
- 拒绝/降级路径：Bot 不存在、余额不足预检拦截、危机干预拦截、LLM 失败不扣费
- Token 计费：结算金额=API usage 总量、预检仅查不扣、usage 缺失跳过结算

安全检查器/情绪检测/回复清洗为纯规则实现，走真实代码；
默认隔离仓储层、上下文管理、prompt 组装、LLM Provider 和后台任务，
跨轮记忆等回归通过 mongomock 使用真实仓库。
"""

import asyncio
from collections.abc import AsyncIterator, Coroutine
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.llm.base import LLMUsage
from app.models.message import MessageInDB
from app.models.service_memory import MemoryEntity, PendingEvent, ServiceMemoryInDB


def _make_bot(**overrides):
    base = dict(id="bot-1", account_id="acc-1", is_blocked=False, block_reason="")
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_identity(**overrides):
    base = dict(
        id="user-1", is_blocked=False, block_reason="",
        is_minor=False, platform_id="real-openid",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_service(**overrides):
    base = dict(
        id="svc-1", identity_id="user-1", account_id="acc-1",
        profile_id="companion", bot_id="bot-1", share_group_id=None,
    )
    base.update(overrides)
    ns = SimpleNamespace(**base)
    # 与 ServiceInDB.context_key 属性行为对齐：共享组优先，否则用服务自身 ID
    ns.context_key = ns.share_group_id or ns.id
    return ns


def _make_profile(**overrides):
    base = dict(
        id="prof-1", slug="companion", voice_id="voice-1",
        is_active=True, visibility="public", account_id="acc-1",
        # 真实 PromptProfileInDB 一定带 language_style.forbidden_words，
        # 放行闸门读取它；替身缺这个字段会让闸门用例测不出真实路径
        language_style=SimpleNamespace(forbidden_words=[]),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_conversation(**overrides):
    base = dict(id="conv-1", summary="", summary_message_count=0, message_count=0)
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_quota_doc(limit: int = 10**9, used: int = 0, expires_at=None):
    """构造额度记录（预检只读 text_quota）。"""
    return SimpleNamespace(
        text_quota=SimpleNamespace(limit=limit, used=used, expires_at=expires_at)
    )


def _build_engine(
    bot=..., identity=..., service=..., profile=..., quota_doc=...,
):
    """构建各依赖均被 mock 的引擎实例（省略参数使用默认正常数据）。"""
    from app.conversation.engine import ConversationEngine

    engine = ConversationEngine("bot-1")

    engine.bot_repo = MagicMock()
    engine.bot_repo.find_by_id = AsyncMock(
        return_value=_make_bot() if bot is ... else bot
    )

    engine.identity_repo = MagicMock()
    engine.identity_repo.find_by_platform = AsyncMock(
        return_value=_make_identity() if identity is ... else identity
    )
    engine.identity_repo.update_last_active = AsyncMock()
    # 按 Bot 记录 openid 映射（主动消息路由），主链路不关心其返回值
    engine.identity_repo.bind_bot_openid = AsyncMock(return_value=False)

    engine.service_repo = MagicMock()
    engine.service_repo.find_by_identity_and_bot = AsyncMock(
        return_value=_make_service() if service is ... else service
    )

    engine.profile_repo = MagicMock()
    engine.profile_repo.find_by_slug = AsyncMock(
        return_value=_make_profile() if profile is ... else profile
    )

    engine.conversation_repo = MagicMock()
    engine.conversation_repo.find_or_create = AsyncMock(
        return_value=_make_conversation()
    )

    engine.quota_repo = MagicMock()
    # 预检（仅查）与结算（扣减）分离：find_by_account 供预检，deduct 供 usage 结算
    engine.quota_repo.find_by_account = AsyncMock(
        return_value=_make_quota_doc() if quota_doc is ... else quota_doc
    )
    engine.quota_repo.deduct = AsyncMock(return_value=(True, ""))
    engine.quota_repo.charge = AsyncMock(return_value=(True, ""))
    engine.quota_repo.refund = AsyncMock(return_value=True)

    engine.turn_recorder = MagicMock()
    engine.turn_recorder.record_identity_message = AsyncMock()
    engine.turn_recorder.record_assistant_message = AsyncMock()
    engine.turn_recorder.update_conversation_meta = AsyncMock()

    engine.memory_repo = MagicMock()
    engine.memory_repo.find_by_service = AsyncMock(return_value=None)
    engine.memory_repo.clear_current_topic = AsyncMock()

    engine.safety_repo = MagicMock()
    engine.safety_repo.create = AsyncMock()

    engine.proactive_repo = MagicMock()
    engine.proactive_repo.get_latest_sent = AsyncMock(return_value=None)

    engine.context_manager = MagicMock()
    engine.context_manager.get_context = AsyncMock(return_value=([], ""))
    engine.context_manager.schedule_summary_if_needed = AsyncMock()

    engine.prompt_assembler = MagicMock()
    engine.prompt_assembler.assemble = MagicMock(
        return_value=[
            {"role": "system", "content": "persona"},
            {"role": "user", "content": "input"},
        ]
    )

    engine.focus_tracker = MagicMock()
    engine.focus_tracker.should_extract = MagicMock(return_value=False)
    engine.focus_tracker.extract_and_save = AsyncMock()

    return engine


def _discard_background(coro: Coroutine[Any, Any, Any]) -> None:
    """关闭被隔离的后台协程，避免访问外部依赖及未等待告警。"""
    coro.close()


def _run(
    engine: Any,
    text: str,
    reply: str = "你好呀，今天过得怎么样？",
    usage: LLMUsage | None = None,
    llm_error: Exception | None = None,
    replies: list[tuple[str, LLMUsage]] | None = None,
    stream: bool = False,
) -> tuple[Any, MagicMock]:
    """同步执行一轮入口，复用异步测试驱动的依赖隔离。"""
    return asyncio.run(_run_async(
        engine, text, reply=reply, usage=usage, llm_error=llm_error,
        replies=replies, stream=stream,
    ))


async def _run_async(
    engine: Any,
    text: str,
    reply: str = "你好呀，今天过得怎么样？",
    usage: LLMUsage | None = None,
    llm_error: Exception | None = None,
    replies: list[tuple[str, LLMUsage]] | None = None,
    stream: bool = False,
) -> tuple[Any, MagicMock]:
    """在同一事件循环内执行入口，兼容 mongomock 测试体。

    replies 传入 [(text, usage), ...] 时按序多次返回（供复读重试用例）。
    """
    provider = MagicMock()
    provider.model_name = "test-model"
    if llm_error is not None:
        provider.complete_with_usage = AsyncMock(side_effect=llm_error)
    elif replies is not None:
        provider.complete_with_usage = AsyncMock(side_effect=replies)
    else:
        provider.complete_with_usage = AsyncMock(
            return_value=(reply, usage if usage is not None else LLMUsage(10, 20))
        )

    async def stream_reply(**kwargs: Any) -> AsyncIterator[str]:
        """仅替换 LLM 网络流，入口编排与回复收尾仍执行真实逻辑。"""
        if llm_error is not None:
            raise llm_error
        stream_usage = usage if usage is not None else LLMUsage(10, 20)
        kwargs["usage_sink"].update(
            prompt_tokens=stream_usage.prompt_tokens,
            completion_tokens=stream_usage.completion_tokens,
        )
        yield reply[:2]
        yield reply[2:]

    provider.stream_complete = MagicMock(side_effect=stream_reply)

    async def collect_stream() -> list[str]:
        """完整消费流，确保生成器末尾的持久化逻辑得到执行。"""
        return [chunk async for chunk in engine.handle_message_stream(
            platform="qq", platform_id="openid-1", text=text,
        )]

    with ExitStack() as stack:
        stack.enter_context(
            patch("app.conversation.engine.get_llm_provider", return_value=provider)
        )
        stack.enter_context(patch(
            "app.conversation.engine.spawn_with_limit", side_effect=_discard_background,
        ))
        for flag in (
            "function_calling_enabled",
            "emotion_llm_fallback_enabled",
            "support_strategy_enabled",
            "consistency_check_enabled",
            "character_mood_enabled",
            "sticker_enabled",
            "embedding_enabled",
        ):
            stack.enter_context(patch.object(settings, flag, False))
        result = await collect_stream() if stream else await engine.handle_message(
            platform="qq", platform_id="openid-1", text=text,
        )
    return result, provider


class TestReceivedAt:
    """普通与流式入口均应保存入口接收时间，而非依赖返回或落库时间。"""

    @pytest.mark.parametrize("stream", [False, True], ids=["normal", "stream"])
    @pytest.mark.parametrize("safety", [False, True], ids=["success", "safety"])
    def test_received_time_survives_io_delay(self, stream: bool, safety: bool) -> None:
        """首个仓储调用后推进时钟，正常收尾和安全分支仍透传旧时间。"""
        engine = _build_engine()
        received_at = datetime(2026, 10, 15, 8, tzinfo=timezone.utc)
        text = "我不想活了" if safety else "今天想出门走走"
        with patch("app.conversation.engine.datetime", wraps=datetime) as clock:
            clock.now.return_value = received_at

            async def find_bot(bot_id: str) -> SimpleNamespace:
                """用依赖延迟区分入口采时和后续采时。"""
                assert bot_id == "bot-1"
                clock.now.return_value = received_at + timedelta(minutes=2)
                return _make_bot()

            engine.bot_repo.find_by_id = AsyncMock(side_effect=find_bot)
            result, provider = _run(engine, text, stream=stream)

        engine.turn_recorder.record_identity_message.assert_awaited_once()
        kwargs = engine.turn_recorder.record_identity_message.await_args.kwargs
        assert kwargs["created_at"] == received_at
        assert kwargs["text"] == text
        engine.turn_recorder.record_assistant_message.assert_awaited_once()
        assistant_kwargs = engine.turn_recorder.record_assistant_message.await_args.kwargs
        assert "created_at" not in assistant_kwargs
        if safety:
            assert "心理援助" in ("".join(result) if stream else result.text)
            assert assistant_kwargs["llm_model"] == "safety_guard"
            provider.complete_with_usage.assert_not_awaited()
            provider.stream_complete.assert_not_called()
        elif stream:
            provider.stream_complete.assert_called_once()
        else:
            provider.complete_with_usage.assert_awaited_once()

    @pytest.mark.parametrize("stream", [False, True], ids=["normal", "stream"])
    def test_focus_receives_persisted_current_turn(self, stream: bool) -> None:
        """焦点输入追加本轮落库对象，保留原始时间且不修改旧历史列表。"""
        engine = _build_engine()
        received_at = datetime(2026, 10, 15, 8, tzinfo=timezone.utc)
        old = MessageInDB(
            _id="old", conversation_id="conv-1", role="user", content="旧话题",
            created_at=received_at - timedelta(minutes=5),
        )
        history = [old]
        identity_msg = MessageInDB(
            _id="saved-user", conversation_id="conv-1", role="user", content="当前输入",
            created_at=received_at,
        )
        assistant_msg = MessageInDB(
            _id="saved-assistant", conversation_id="conv-1", role="assistant",
            content="当前回复", created_at=received_at + timedelta(seconds=3),
        )
        engine.context_manager.get_context.return_value = (history, "")
        engine.turn_recorder.record_identity_message.return_value = identity_msg
        engine.turn_recorder.record_assistant_message.return_value = assistant_msg
        engine.focus_tracker.should_extract.return_value = True
        with patch("app.conversation.engine.datetime", wraps=datetime) as clock:
            clock.now.return_value = received_at
            _run(engine, "当前输入", reply="当前回复", stream=stream)

        # 后台调度被隔离，但任务创建时的入参必须是真实落库返回值。
        engine.focus_tracker.extract_and_save.assert_called_once()
        args = engine.focus_tracker.extract_and_save.call_args.args
        assert args[0] == "svc-1"
        extracted = args[1]
        assert len(extracted) == 3
        assert extracted[0] is old
        assert extracted[1] is identity_msg
        assert extracted[2] is assistant_msg
        assert extracted[1].created_at == received_at
        assert extracted[2].created_at > received_at
        assert history == [old] and extracted is not history

    def test_focus_throttle_uses_explicit_extract_time(self) -> None:
        """节流必须读 last_focus_extract_at，而不是被每轮刷新的 updated_at。"""
        engine = _build_engine()
        recent_updated = datetime(2026, 10, 15, 8, tzinfo=timezone.utc)
        old_extract = recent_updated - timedelta(hours=2)
        memory = ServiceMemoryInDB(
            _id="memory-1",
            service_id="svc-1",
            updated_at=recent_updated,
            last_focus_extract_at=old_extract,
        )
        engine.memory_repo.find_by_service = AsyncMock(return_value=memory)

        _run(engine, "普通消息")

        kwargs = engine.focus_tracker.should_extract.call_args.kwargs
        assert kwargs["last_extract_time"] == old_extract
        assert kwargs["last_extract_time"] != recent_updated


class TestGapMode:
    """间隔分类使用运行时阈值，含临界秒及时间总开关。"""

    @pytest.mark.parametrize(("seconds", "expected"), [
        (-1, "none"), (0, "none"), (8 * 3600 - 1, "none"),
        (8 * 3600, "session"), (8 * 3600 + 1, "session"),
        (72 * 3600 - 1, "session"), (72 * 3600, "reunion"),
        (72 * 3600 + 1, "reunion"),
    ])
    def test_default_boundaries(self, seconds: float, expected: str) -> None:
        """软档八小时、强档七十二小时均包含等号。"""
        from app.conversation.engine import _gap_mode

        with patch.multiple(
            settings, message_time_enabled=True,
            time_session_gap_hours=8, time_reunion_gap_hours=72,
        ):
            assert _gap_mode(seconds) == expected

    @pytest.mark.parametrize(("hours", "expected"), [
        (1, "none"), (2, "session"), (4, "session"), (5, "reunion"),
    ])
    def test_runtime_thresholds(self, hours: int, expected: str) -> None:
        """自定义阈值热更新生效，不能硬编码默认值。"""
        from app.conversation.engine import _gap_mode

        with patch.multiple(
            settings, message_time_enabled=True,
            time_session_gap_hours=2, time_reunion_gap_hours=5,
        ):
            assert _gap_mode(hours * 3600) == expected

    def test_master_switch_overrides_all_gaps(self) -> None:
        """总开关关闭后，无论间隔多长都不分档。"""
        from app.conversation.engine import _gap_mode

        with patch.object(settings, "message_time_enabled", False):
            for hours in (0, 8, 72, 1000):
                assert _gap_mode(hours * 3600) == "none"


class TestGapTopicIntegration:
    """两入口在真实记忆检索后重置当前话题，长间隔不再触发停滞引导。"""

    @pytest.mark.parametrize("stream", [False, True], ids=["normal", "stream"])
    @pytest.mark.parametrize(("hours", "enabled", "mode"), [
        (1, True, "none"), (8, True, "session"), (72, True, "reunion"),
        (72, False, "none"),
    ])
    def test_topic_reset_and_stall_gate(
        self, stream: bool, hours: int, enabled: bool, mode: str,
    ) -> None:
        """近期 assistant 不重置身份空闲时间，短间隔仍保留既有停滞行为。"""
        engine = _build_engine()
        received_at = datetime(2026, 10, 15, 8, tzinfo=timezone.utc)
        last_identity_at = received_at - timedelta(hours=hours)
        history = [MessageInDB(
            _id=f"old-user-{i}", conversation_id="conv-1", role="user",
            content="一直在讨论考试", created_at=last_identity_at - timedelta(minutes=3 - i),
        ) for i in range(4)]
        history.append(MessageInDB(
            _id="recent-assistant", conversation_id="conv-1", role="assistant",
            content="昨天的回复", created_at=received_at - timedelta(seconds=1),
        ))
        memory = ServiceMemoryInDB(
            _id="memory-1", service_id="svc-1", current_topic="考试",
            topic_history=["电影", "旅行"],
            entities=[MemoryEntity(
                name="小红", relation_to_identity="朋友", attributes={"爱好": "摄影"},
            )],
            pending_events=[PendingEvent(
                content="参加面试", due_at=received_at + timedelta(days=1),
            )],
        )
        engine.context_manager.get_context.return_value = (history, "")
        engine.memory_repo.find_by_service.return_value = memory
        engine.proactive_repo.get_latest_sent.return_value = SimpleNamespace(
            sent_at=received_at - timedelta(minutes=10), content="我来问候你",
        )
        with (
            patch("app.conversation.engine.datetime", wraps=datetime) as clock,
            patch.multiple(
                settings, message_time_enabled=enabled,
                time_session_gap_hours=8, time_reunion_gap_hours=72,
                topic_stall_detection_enabled=True, topic_stall_threshold=4,
                topic_stall_similarity=0.6,
            ),
            patch.object(
                engine.topic_manager, "generate_topic_context",
                wraps=engine.topic_manager.generate_topic_context,
            ) as generate_topic,
            patch.object(
                engine.topic_manager, "detect_topic_stall",
                wraps=engine.topic_manager.detect_topic_stall,
            ) as detect_stall,
        ):
            clock.now.return_value = received_at
            _run(engine, "今天想聊聊小红", stream=stream)

        engine.memory_repo.find_by_service.assert_awaited_once_with("svc-1")
        kwargs = engine.prompt_assembler.assemble.call_args.kwargs
        assert kwargs["gap_mode"] == mode
        assert "电影→旅行" in kwargs["topic_context"]
        # 长间隔只重置话题前缀，后续实体和待跟进事件必须继续注入。
        assert "【提到的人物/事物】\n小红（对方的朋友）；爱好=摄影" in kwargs["topic_context"]
        assert "【待跟进的事】" in kwargs["topic_context"]
        assert "参加面试" in kwargs["topic_context"]
        if mode == "none":
            engine.memory_repo.clear_current_topic.assert_not_awaited()
            assert kwargs["current_topic"] == "考试"
            assert "当前：考试" in kwargs["topic_context"]
            assert "话题引导" in kwargs["topic_stall_hint"]
            detect_stall.assert_called_once()
            generate_topic.assert_called_once_with(memory.topic_history, "考试")
            engine.proactive_repo.get_latest_sent.assert_not_awaited()
            assert kwargs["last_proactive_at"] is None
            assert kwargs["last_proactive_text"] == ""
        else:
            engine.memory_repo.clear_current_topic.assert_awaited_once_with("svc-1", "考试")
            assert kwargs["current_topic"] == ""
            assert "当前：" not in kwargs["topic_context"]
            assert kwargs["topic_stall_hint"] == ""
            detect_stall.assert_not_called()
            assert generate_topic.call_count == 3
            generate_topic.assert_called_with(memory.topic_history, "")
            assert kwargs["last_proactive_at"] == received_at - timedelta(minutes=10)
            assert kwargs["last_proactive_text"] == "我来问候你"
            engine.proactive_repo.get_latest_sent.assert_awaited_once_with(
                identity_id="user-1", bot_id="bot-1",
            )
        assert memory.current_topic == "考试"  # 清理持久化焦点，不篡改已返回的记忆对象。

    @pytest.mark.parametrize("current_topic", ["", "考试"], ids=["empty-topic", "topic"])
    @pytest.mark.parametrize("topic_history", [[], ["电影", "旅行"]], ids=["empty-history", "history"])
    def test_reset_only_replaces_topic_prefix(
        self, current_topic: str, topic_history: list[str],
    ) -> None:
        """无话题前缀时仍保留上下文，尾部出现相同前缀时也不能被全局替换。"""
        engine = _build_engine()
        old_prefix = engine.topic_manager.generate_topic_context(topic_history, current_topic)
        new_prefix = engine.topic_manager.generate_topic_context(topic_history, "")
        suffix = (
            "【提到的人物/事物】\n小红（对方的朋友）；备注="
            f"{old_prefix or '记录原文'}\n\n【待跟进的事】\n- 参加面试"
        )
        context = f"{old_prefix}\n\n{suffix}" if old_prefix else suffix
        result = asyncio.run(engine._reset_topic_after_gap(
            "svc-1", current_topic, topic_history, context,
        ))
        assert result == (f"{new_prefix}\n\n{suffix}" if old_prefix else suffix)
        if current_topic:
            engine.memory_repo.clear_current_topic.assert_awaited_once_with("svc-1", current_topic)
        else:
            engine.memory_repo.clear_current_topic.assert_not_awaited()
            assert result == context

    @pytest.mark.parametrize("stream", [False, True], ids=["normal", "stream"])
    @pytest.mark.parametrize(("hours", "mode"), [(8, "session"), (72, "reunion")])
    def test_cleared_topic_does_not_return_without_focus_extraction(
        self, stream: bool, hours: int, mode: str,
    ) -> None:
        """真实仓库串联两轮：首轮清空焦点，下一轮无间隔也不会重新读到旧话题。"""
        from app.conversation.turn_recorder import TurnRecorder
        from app.db.repositories.service_memory_repo import ServiceMemoryRepository
        from tools.test.db_test_utils import run_with_db

        async def body() -> None:
            """隔离外部依赖和后台任务，记忆读写与双向消息落库使用 mongomock。"""
            engine = _build_engine(service=_make_service(share_group_id="grp-1"))
            engine.conversation_repo.find_or_create_by_key = AsyncMock(
                return_value=_make_conversation(),
            )
            repo = ServiceMemoryRepository()
            engine.memory_repo = repo
            await repo.upsert_topic("grp-1", "考试")
            await repo.append_topic_history("grp-1", "电影")
            before = await repo.find_by_service("grp-1")
            assert before is not None and before.current_topic == "考试"

            received_at = datetime(2026, 10, 15, 8, tzinfo=timezone.utc)
            recorder = TurnRecorder()
            engine.turn_recorder.record_identity_message = AsyncMock(wraps=recorder.record_identity_message)
            engine.turn_recorder.record_assistant_message = AsyncMock(
                wraps=recorder.record_assistant_message,
            )
            await recorder.message_repo.create(
                "conv-1", "user", "之前一直在讨论考试",
                created_at=received_at - timedelta(hours=hours),
            )

            async def get_context(
                conversation_id: str, summary: str, emotion_intensity: float,
            ) -> tuple[list[MessageInDB], str]:
                """每轮重读真实消息，第二轮间隔必须来自首轮已保存的对方消息。"""
                return await recorder.message_repo.get_recent(conversation_id), summary

            engine.context_manager.get_context.side_effect = get_context
            with (
                patch("app.conversation.engine.datetime", wraps=datetime) as clock,
                patch("app.db.repositories.message_repo.datetime", wraps=datetime) as message_clock,
                patch.object(repo, "clear_current_topic", wraps=repo.clear_current_topic) as clear_topic,
                patch.multiple(
                    settings, message_time_enabled=True,
                    time_session_gap_hours=8, time_reunion_gap_hours=72,
                ),
            ):
                for index, (text, reply, expected_mode) in enumerate([
                    ("今天想去公园", "去公园散步很放松。", mode),
                    ("打算在那儿待一会儿", "也可以在树荫下坐坐。", "none"),
                ]):
                    clock.now.return_value = received_at + timedelta(minutes=index)
                    message_clock.now.return_value = clock.now.return_value + timedelta(seconds=3)
                    result, provider = await _run_async(engine, text, reply=reply, stream=stream)
                    assert ("".join(result) if stream else result.text) == reply
                    if stream:
                        provider.stream_complete.assert_called_once()
                    else:
                        provider.complete_with_usage.assert_awaited_once()
                    kwargs = engine.prompt_assembler.assemble.call_args.kwargs
                    assert kwargs["gap_mode"] == expected_mode
                    assert kwargs["current_topic"] == ""
                    assert kwargs["topic_context"] == "【对话话题脉络】之前聊过：电影"
                    stored = await repo.find_by_service("grp-1")
                    assert stored is not None
                    assert stored.model_dump() == before.model_copy(
                        update={"current_topic": ""},
                    ).model_dump()

                clear_topic.assert_awaited_once_with("grp-1", "考试")
            assert engine.focus_tracker.should_extract.call_count == 2
            engine.focus_tracker.extract_and_save.assert_not_called()
            assert await recorder.message_repo.count_by_conversation("conv-1") == 5
            saved_identities = [
                message for message in await recorder.message_repo.get_recent("conv-1")
                if message.role == "user"
            ]
            assert saved_identities[1].created_at.replace(tzinfo=timezone.utc) == received_at
            assert saved_identities[2].created_at.replace(tzinfo=timezone.utc) == received_at + timedelta(minutes=1)

        run_with_db(body)

    @pytest.mark.parametrize("stream", [False, True], ids=["normal", "stream"])
    @pytest.mark.parametrize("history_kind", ["empty", "assistant", "untimed-user"])
    def test_missing_identity_time_does_not_invent_gap(
        self, stream: bool, history_kind: str,
    ) -> None:
        """无真实身份时间时不能凭 assistant 时间或当前时间推断久别。"""
        engine = _build_engine()
        history: list[Any] = []
        if history_kind == "assistant":
            history.append(MessageInDB(
                _id="old-assistant", conversation_id="conv-1", role="assistant",
                content="旧回复", created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            ))
        elif history_kind == "untimed-user":
            history.append(SimpleNamespace(role="user", content="旧数据", emotion_tag=None))
        engine.context_manager.get_context.return_value = (history, "")
        with patch.object(settings, "message_time_enabled", True):
            _run(engine, "重新聊聊", stream=stream)
        assert engine.prompt_assembler.assemble.call_args.kwargs["gap_mode"] == "none"
        engine.proactive_repo.get_latest_sent.assert_not_awaited()


class TestRecentProactiveContext:
    """主动消息仅在最后对方消息之后、当前接收时间之前参与长间隔上下文。"""

    @pytest.mark.parametrize("mode", ["session", "reunion"])
    @pytest.mark.parametrize("naive", [False, True], ids=["aware", "mongo-naive"])
    @pytest.mark.parametrize(("offset", "included"), [
        (-1, False), (0, False), (1, True), (3600, True), (3601, False), (None, False),
    ])
    def test_time_window_boundaries(
        self, mode: str, naive: bool, offset: int | None, included: bool,
    ) -> None:
        """下界严格排除，上界包含；MongoDB 无时区发送时间按 UTC 处理。"""
        engine = _build_engine()
        last_identity_at = datetime(2026, 10, 15, 8, tzinfo=timezone.utc)
        received_at = last_identity_at + timedelta(hours=1)
        sent_at = last_identity_at + timedelta(seconds=offset) if offset is not None else None
        stored_at = sent_at.replace(tzinfo=None) if sent_at and naive else sent_at
        engine.proactive_repo.get_latest_sent.return_value = SimpleNamespace(
            sent_at=stored_at, content="[10-15 08:30] 我来问候你",
        )
        result = asyncio.run(engine._recent_proactive_context(
            "user-1", last_identity_at, received_at, mode,
        ))
        assert result == ((sent_at, "我来问候你") if included else (None, ""))
        engine.proactive_repo.get_latest_sent.assert_awaited_once_with(
            identity_id="user-1", bot_id="bot-1",
        )

    @pytest.mark.parametrize(("mode", "has_identity_time"), [
        ("none", True), ("session", False), ("reunion", False),
    ])
    def test_unneeded_query_is_skipped(self, mode: str, has_identity_time: bool) -> None:
        """无分档或无身份时间不查主动记录，避免多余查询与错误注入。"""
        engine = _build_engine()
        now = datetime(2026, 10, 15, tzinfo=timezone.utc)
        result = asyncio.run(engine._recent_proactive_context(
            "user-1", now - timedelta(hours=8) if has_identity_time else None, now, mode,
        ))
        assert result == (None, "")
        engine.proactive_repo.get_latest_sent.assert_not_awaited()

    def test_only_current_identity_and_bot_sent_record_is_used(self) -> None:
        """用真实主动消息仓库验证身份、Bot、发送状态的隔离。"""
        from app.conversation.engine import ConversationEngine
        from app.models.proactive_record import ProactiveRecord
        from tools.test.db_test_utils import run_with_db

        async def body() -> None:
            """其他 Bot、其他身份或失败记录即使更新也不能覆盖有效问候。"""
            engine = ConversationEngine("bot-1")
            received_at = datetime(2026, 10, 15, 8, tzinfo=timezone.utc)
            last_identity_at = received_at - timedelta(hours=8)
            assert await engine._recent_proactive_context(
                "user-1", last_identity_at, received_at, "session",
            ) == (None, "")
            records: list[tuple[str, str, Literal["sent", "failed"], str]] = [
                ("user-1", "bot-1", "sent", "本轮有效问候"),
                ("user-1", "bot-2", "sent", "其他机器人"),
                ("user-2", "bot-1", "sent", "其他身份"),
                ("user-1", "bot-1", "failed", "失败不能注入"),
            ]
            for index, (identity_id, bot_id, status, content) in enumerate(records):
                sent_at = last_identity_at + timedelta(minutes=index + 1)
                await engine.proactive_repo.create(ProactiveRecord(
                    identity_id=identity_id, platform_openid="openid-1", bot_id=bot_id,
                    stage=1, status=status, content=content,
                    created_at=sent_at, sent_at=sent_at,
                ))
            assert await engine._recent_proactive_context(
                "user-1", last_identity_at, received_at, "session",
            ) == (last_identity_at + timedelta(minutes=1), "本轮有效问候")

        run_with_db(body)


class TestHappyPath:
    """正常主路径：校验→解析→预检→LLM→usage 结算→持久化 全链路不变量。"""

    def test_full_turn_invariants(self) -> None:
        engine = _build_engine()
        text = "今天天气真好呀"
        reply = "是呀，出去走走吧！"
        usage = LLMUsage(prompt_tokens=35, completion_tokens=45)

        result, provider = _run(engine, text, reply=reply, usage=usage)

        # 回复与关联信息正确
        assert result.text == reply
        assert result.conversation_id == "conv-1"
        assert result.account_id == "acc-1"
        assert result.voice_id == "voice-1"

        # LLM 恰好调用一次
        provider.complete_with_usage.assert_awaited_once()

        # Token 结算：仅一次记账，金额 = API usage 总量（非字符数）
        charged = engine.quota_repo.charge.await_args
        assert charged.args[:3] == ("acc-1", "text", usage.total)

        # 双向消息均持久化，且 AI 回复带模型名
        engine.turn_recorder.record_identity_message.assert_awaited_once()
        assert (
            engine.turn_recorder.record_identity_message.await_args
            .kwargs["text"] == text
        )
        engine.turn_recorder.record_assistant_message.assert_awaited_once()
        assistant_kwargs = (
            engine.turn_recorder.record_assistant_message.await_args.kwargs
        )
        assert assistant_kwargs["clean_text"] == reply
        assert assistant_kwargs["llm_model"] == "test-model"

        # 会话元数据与身份活跃时间已刷新
        engine.turn_recorder.update_conversation_meta.assert_awaited_once_with(
            "conv-1"
        )
        engine.identity_repo.update_last_active.assert_awaited_once_with("user-1")

        # 按 Bot 记录 openid 映射（主动消息需按 Bot 路由发送目标）
        engine.identity_repo.bind_bot_openid.assert_awaited_once_with(
            "user-1", "bot-1", "openid-1"
        )


class TestAutoBindOpenid:
    """未绑定身份自动绑定分支：platform_id 与 Bot 维度映射同步写入。"""

    def test_bind_platform_id_and_bot_openid(self) -> None:
        engine = _build_engine(identity=None)
        # 预创建身份（platform_id 为 UUID = 未绑定），服务按 Bot 反查
        engine.identity_repo.find_by_id = AsyncMock(
            return_value=_make_identity(
                platform_id="12345678-1234-1234-1234-123456789abc"
            )
        )
        engine.identity_repo.bind_platform_id = AsyncMock()
        engine.service_repo.find_active_by_bot = AsyncMock(
            return_value=_make_service()
        )

        _run(engine, "你好")

        engine.identity_repo.bind_platform_id.assert_awaited_once_with(
            "user-1", "openid-1", "", platform="qq"
        )
        engine.identity_repo.bind_bot_openid.assert_awaited_once_with(
            "user-1", "bot-1", "openid-1"
        )


class TestQuotaDenied:
    """余额不足：预检拦截（仅查不扣），不调 LLM、不持久化。"""

    def test_denied_before_llm(self) -> None:
        # 余额为 0 → 低于预估输入+输出上限，预检拒绝
        engine = _build_engine(quota_doc=_make_quota_doc(limit=100, used=100))

        result, provider = _run(engine, "随便聊聊")

        assert result.text == "额度不足：额度不足"
        assert result.conversation_id == "conv-1"
        provider.complete_with_usage.assert_not_awaited()
        engine.quota_repo.deduct.assert_not_awaited()
        engine.turn_recorder.record_identity_message.assert_not_awaited()
        engine.turn_recorder.record_assistant_message.assert_not_awaited()


class TestSafetyCrisis:
    """危机干预：命中真实安全检查器，不扣额度、不调 LLM，留痕告警。"""

    def test_crisis_bypasses_quota_and_llm(self) -> None:
        engine = _build_engine()

        result, provider = _run(engine, "我不想活了")

        # 返回心理援助话术，且不消耗额度、不调用 LLM
        assert "心理援助" in result.text
        engine.quota_repo.deduct.assert_not_awaited()
        provider.complete_with_usage.assert_not_awaited()

        # 安全事件落库 + 对话留痕（AI 侧标记为 safety_guard）
        engine.safety_repo.create.assert_awaited_once()
        assistant_kwargs = (
            engine.turn_recorder.record_assistant_message.await_args.kwargs
        )
        assert assistant_kwargs["llm_model"] == "safety_guard"
        assert result.account_id == "acc-1"


class TestLLMFailure:
    """LLM 调用失败：返回兜底话术；预检未预扣故不扣费、无需退款。"""

    def test_no_charge_on_llm_error(self) -> None:
        engine = _build_engine()
        text = "给我讲个故事吧"

        result, provider = _run(engine, text, llm_error=RuntimeError("boom"))

        assert result.text == "网络有点开小差了，你可以再发一次吗？"
        engine.quota_repo.deduct.assert_not_awaited()
        engine.quota_repo.refund.assert_not_awaited()
        engine.turn_recorder.record_identity_message.assert_not_awaited()
        engine.turn_recorder.record_assistant_message.assert_not_awaited()


class TestTokenBilling:
    """Token 计费语义：结算金额=usage 总量、预检拦截、零 usage 跳过。"""

    def test_settle_amount_equals_usage_total(self) -> None:
        """结算扣费金额必须等于 prompt+completion 总量。"""
        engine = _build_engine()
        usage = LLMUsage(prompt_tokens=128, completion_tokens=64)

        _run(engine, "聊聊最近的事", usage=usage)

        charged = engine.quota_repo.charge.await_args
        assert charged.args[:3] == ("acc-1", "text", 192)

    def test_precheck_threshold_blocks_llm(self, monkeypatch) -> None:
        """余额低于 预估输入 token + 输出上限 → 拒绝且不调用 LLM。"""
        monkeypatch.setattr(settings, "chat_max_tokens", 2048)
        # 余额 2050 < estimate_tokens("你好")(3) + chat_max_tokens(2048) = 2051
        engine = _build_engine(quota_doc=_make_quota_doc(limit=4098, used=2048))

        result, provider = _run(engine, "你好")

        assert result.text.startswith("额度不足")
        provider.complete_with_usage.assert_not_awaited()
        engine.quota_repo.deduct.assert_not_awaited()

    def test_zero_usage_charges_estimate(self) -> None:
        """后端未返回 usage（零值）：按 Prompt+回复估算记账，不再免费。"""
        engine = _build_engine()

        result, _ = _run(
            engine, "在吗", usage=LLMUsage(0, 0), reply="在的呀"
        )

        assert result.text == "在的呀"
        engine.quota_repo.deduct.assert_not_awaited()
        charged = engine.quota_repo.charge.await_args
        assert charged.args[0:2] == ("acc-1", "text")
        assert charged.args[2] > 0

    def test_precise_precheck_counts_prompt(self, monkeypatch) -> None:
        """Prompt 组装后的预检必须计入 system/历史等完整上下文。"""
        monkeypatch.setattr(settings, "chat_max_tokens", 2048)
        engine = _build_engine(quota_doc=_make_quota_doc(limit=2500, used=0))

        ok, reason = asyncio.run(engine._quota_precheck_for_messages(
            "acc-1", [{"content": "你" * 1000}]
        ))

        assert ok is False and reason == "额度不足"


class TestBotNotFound:
    """Bot 不存在：最先拒绝，不触发任何后续查询。"""

    def test_rejects_before_user_lookup(self) -> None:
        engine = _build_engine(bot=None)

        result, provider = _run(engine, "在吗")

        assert result.text == "该渠道还没有配置对话服务。登录网页端「聊天服务」页添加渠道后，再创建一个服务即可开始对话。"
        assert result.conversation_id == ""
        engine.identity_repo.find_by_platform.assert_not_awaited()
        provider.complete_with_usage.assert_not_awaited()


class TestNoProfile:
    """无可用角色：不再默认兜底，提示创建角色且不消耗额度、不调 LLM。"""

    def test_prompts_create_profile(self) -> None:
        engine = _build_engine(profile=None)

        result, provider = _run(engine, "在吗")

        assert "创建角色" in result.text
        provider.complete_with_usage.assert_not_awaited()
        engine.quota_repo.deduct.assert_not_awaited()


def _make_history(*assistant_texts: str):
    """构造含 assistant 消息的上下文历史（仅保留主链路访问的字段）。"""
    history = []
    for t in assistant_texts:
        history.append(SimpleNamespace(
            role="user", content="对方消息", emotion_tag=None,
        ))
        history.append(SimpleNamespace(
            role="assistant", content=t, emotion_tag=None,
        ))
    return history


class TestReplyJitterDetection:
    """回复抖动检测：复读命中重试一次，重试仍复读则照常发送不死循环。"""

    def test_repeat_triggers_single_retry(self) -> None:
        """首次回复与上一条 assistant 消息雷同 → 重试一次并采用新回复。"""
        engine = _build_engine()
        repeated = "哇！看着好好吃呀！哪家店的外卖？"
        fresh = "又发了一遍呀，确实看着很香呢！"
        engine.context_manager.get_context = AsyncMock(
            return_value=(_make_history(repeated), "")
        )

        result, provider = _run(
            engine, "[表情]",
            replies=[
                (repeated, LLMUsage(10, 20)),
                (fresh, LLMUsage(15, 25)),
            ],
        )

        assert result.text == fresh
        assert provider.complete_with_usage.await_count == 2
        # 重试请求末尾追加了防复读系统提示，温度上浮不超 1.0
        retry_kwargs = provider.complete_with_usage.await_args_list[1].kwargs
        assert retry_kwargs["messages"][-1]["role"] == "system"
        assert retry_kwargs["temperature"] <= 1.0
        # 计费以重试后的真实 usage 结算（不重复扣首次消耗）
        charged = engine.quota_repo.charge.await_args
        assert charged.args[:3] == ("acc-1", "text", 40)

    def test_still_repeat_after_retry_sends_anyway(self) -> None:
        """重试后仍复读：重试 N 次后仍复读则照常发送并记日志。"""
        engine = _build_engine()
        repeated = "这条回复被模型反复复读输出。"
        engine.context_manager.get_context = AsyncMock(
            return_value=(_make_history(repeated), "")
        )

        result, provider = _run(
            engine, "你好",
            replies=[
                (repeated, LLMUsage(10, 20)),
                (repeated, LLMUsage(10, 20)),
                (repeated, LLMUsage(10, 20)),
            ],
        )

        assert result.text == repeated
        # 最多重试 reply_jitter_retry_max 次（默认 2），总调用 = 1 + 2 = 3
        assert provider.complete_with_usage.await_count == 3
        engine.quota_repo.charge.assert_awaited_once()

    def test_no_history_no_retry(self) -> None:
        """无历史 assistant 消息：即使开关开启也不触发重试。"""
        engine = _build_engine()

        result, provider = _run(engine, "在吗", reply="在的呀，怎么啦？")

        assert result.text == "在的呀，怎么啦？"
        provider.complete_with_usage.assert_awaited_once()

    def test_detection_disabled_no_retry(self) -> None:
        """开关关闭：复读不拦截，只调一次。"""
        engine = _build_engine()
        repeated = "开关关闭时不检测复读。"
        engine.context_manager.get_context = AsyncMock(
            return_value=(_make_history(repeated), "")
        )

        with patch.object(settings, "reply_jitter_detection_enabled", False):
            result, provider = _run(engine, "嗯嗯", reply=repeated)

        assert result.text == repeated
        provider.complete_with_usage.assert_awaited_once()



class TestStreamGateAndPacing:
    """放行闸门与节奏：清洗前移到放行之前，气泡边界与停顿由服务端控。

    默认开关全关，因此本类必须显式打开；关掉时"逐字节等价改动前"由
    test_stream_frames.py 与上面的既有用例共同守住。
    """

    @staticmethod
    def _collect(items: list[Any]) -> tuple[list[str], list[str]]:
        """把引擎产出拆成 (控制帧序列, 正文序列)。"""
        kinds = [
            "msg" if isinstance(i, str) else getattr(i, "kind", "") for i in items
        ]
        texts = [i if isinstance(i, str) else getattr(i, "text", "") for i in items]
        return kinds, [t for t in texts if t]

    def test_gate_off_yields_plain_str(self) -> None:
        """闸门关闭时不得混入 StreamEvent——老前端只认裸文本帧。"""
        engine = _build_engine()
        result, _ = _run(engine, "今天想出门走走", stream=True)
        assert all(isinstance(i, str) for i in result)
        assert "".join(result) == "你好呀，今天过得怎么样？"

    def test_stream_disconnect_settles_exactly_once(self) -> None:
        """客户端在结算途中断开：后台结算继续完成，且不能补第二次。"""
        async def body() -> None:
            engine = _build_engine()
            started = asyncio.Event()
            finished = asyncio.Event()

            async def slow_settle(*_args: Any, **_kwargs: Any) -> None:
                started.set()
                await asyncio.sleep(0.05)
                finished.set()

            engine._settle_usage = AsyncMock(side_effect=slow_settle)
            engine._finalize_turn = AsyncMock()

            provider = MagicMock()
            provider.model_name = "test-model"

            async def stream_reply(**kwargs: Any) -> AsyncIterator[str]:
                kwargs["usage_sink"].update(prompt_tokens=1, completion_tokens=2)
                yield "你"
                yield "好"

            provider.stream_complete = MagicMock(side_effect=stream_reply)
            with patch(
                "app.conversation.engine.get_llm_provider", return_value=provider
            ):
                agen = engine.handle_message_stream(
                    platform="qq", platform_id="openid-1", text="hi"
                )
                assert await agen.__anext__() == "你"
                assert await agen.__anext__() == "好"
                pending = asyncio.create_task(agen.__anext__())
                await asyncio.wait_for(started.wait(), timeout=1)
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
                await agen.aclose()
                await asyncio.wait_for(finished.wait(), timeout=1)

            assert engine._settle_usage.await_count == 1
            assert engine._finalize_turn.await_count == 1

        asyncio.run(body())

    def test_markers_never_reach_the_screen(self) -> None:
        """（微笑）/[表情:]/[IMG:] 必须在放行之前就被删掉。"""
        engine = _build_engine()
        dirty = "辛苦啦（微笑）[表情:一个抱抱]早点休息哦。[IMG:窗边的猫]"
        with patch.object(settings, "stream_gate_enabled", True):
            result, _ = _run(engine, "今天好累", reply=dirty, stream=True)
        _, texts = self._collect(result)
        joined = "".join(texts)
        assert "（微笑）" not in joined
        assert "[表情" not in joined and "[IMG" not in joined
        assert "辛苦啦" in joined and "早点休息" in joined

    def test_stored_text_equals_released_text(self) -> None:
        """所见即所存：落库文本必须就是屏幕上那份，而不是重新清洗的原文。"""
        engine = _build_engine()
        dirty = "在的呀（偷笑）今天怎么这么晚。"
        with patch.object(settings, "stream_gate_enabled", True):
            result, _ = _run(engine, "在吗", reply=dirty, stream=True)
        _, texts = self._collect(result)
        stored = engine.turn_recorder.record_assistant_message.await_args.kwargs["clean_text"]
        assert stored == "\n".join(texts)
        assert "（偷笑）" not in stored

    def test_ai_tells_stripped_before_release(self) -> None:
        """助手腔整句在放行前就被剔除，不会先显示再消失。"""
        engine = _build_engine()
        with patch.object(settings, "stream_gate_enabled", True):
            result, _ = _run(
                engine, "帮我看看",
                reply="当然可以。我陪你一起想办法。", stream=True,
            )
        _, texts = self._collect(result)
        joined = "".join(texts)
        assert "当然可以" not in joined
        assert "我陪你一起想办法" in joined

    def test_pacing_off_does_not_call_rhythm(self) -> None:
        """节奏未开启：不得计算停顿，也不得 sleep。"""
        engine = _build_engine()
        sleep_mock = AsyncMock()
        with patch.object(settings, "stream_gate_enabled", True), patch(
            "app.conversation.engine.message_gap_ms", side_effect=AssertionError
        ), patch("app.conversation.engine.asyncio.sleep", sleep_mock):
            _run(engine, "在吗", reply="在的呀。怎么啦？", stream=True)
        sleep_mock.assert_not_awaited()

    def test_pacing_on_inserts_gaps_between_bubbles(self) -> None:
        """节奏开启：首条走 thinking 延迟，后续走气泡间隔，且都真的 sleep。"""
        engine = _build_engine()
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        with patch.object(settings, "stream_gate_enabled", True), patch.object(
            settings, "stream_pacing_enabled", True
        ), patch.object(settings, "human_rhythm_enabled", True), patch(
            "app.conversation.engine.thinking_ms", return_value=800
        ), patch(
            "app.conversation.engine.message_gap_ms", return_value=1500
        ), patch(
            "app.conversation.engine.asyncio.sleep", side_effect=fake_sleep
        ):
            result, _ = _run(engine, "在吗", reply="在的呀。怎么啦？", stream=True)
        assert sleeps == [0.8, 1.5]
        assert len(self._collect(result)[1]) == 2

    def test_delay_budget_guard(self) -> None:
        """总延迟护栏到顶后不再 sleep，长回复不能被节奏拖死。"""
        engine = _build_engine()
        sleep_mock = AsyncMock()
        with patch.object(settings, "stream_gate_enabled", True), patch.object(
            settings, "stream_pacing_enabled", True
        ), patch.object(settings, "human_rhythm_enabled", True), patch.object(
            settings, "stream_max_total_delay_seconds", 0.0
        ), patch(
            "app.conversation.engine.message_gap_ms", return_value=1500
        ), patch(
            "app.conversation.engine.asyncio.sleep", sleep_mock
        ):
            _run(engine, "在吗", reply="在的呀。怎么啦？", stream=True)
        sleep_mock.assert_not_awaited()

    def test_all_dropped_releases_fallback(self) -> None:
        """整段都是越界内容时兜底一句，屏幕不能空着。"""
        engine = _build_engine()
        with patch.object(settings, "stream_gate_enabled", True):
            result, _ = _run(
                engine, "你是谁", reply="我是AI助手。作为语言模型我只能这样说。",
                stream=True,
            )
        _, texts = self._collect(result)
        assert texts == ["刚才那句我没说好，你再说一次？"]

    def test_client_abort_still_persists(self) -> None:
        """客户端中止：已放行部分交给后台任务落库，不留在空气泡里。"""
        engine = _build_engine()
        spawned: list[Any] = []
        agen = None

        async def flow() -> None:
            nonlocal agen
            agen = engine.handle_message_stream(
                platform="qq", platform_id="openid-1", text="在吗"
            )
            first = await agen.__anext__()
            assert first is not None
            await agen.aclose()

        with patch.object(settings, "stream_gate_enabled", True), patch(
            "app.conversation.engine.spawn",
            side_effect=lambda coro: (spawned.append(coro), MagicMock())[0],
        ):
            with patch("app.conversation.engine.get_llm_provider") as get_provider:
                provider = MagicMock()
                provider.model_name = "test-model"

                async def stream_reply(**kwargs: Any) -> AsyncIterator[str]:
                    kwargs["usage_sink"].update(prompt_tokens=1, completion_tokens=2)
                    yield "在的呀。"
                    yield "怎么啦？"

                provider.stream_complete = MagicMock(side_effect=stream_reply)
                get_provider.return_value = provider
                with patch(
                    "app.conversation.engine.spawn_with_limit",
                    side_effect=_discard_background,
                ):
                    asyncio.run(flow())

        # 中止路径必须把落库交给后台任务（在 finally 里 await 会触发 GeneratorExit 冲突）
        assert len(spawned) == 1
        spawned[0].close()  # 关掉未执行的协程，避免 RuntimeWarning


class TestStreamRepeatGuard:
    """流式复读护栏：边放行边判，命中即 [RESET] 收回并重跑一次。"""

    OLD = "今天天气真的很好呀，我们一起去公园走走怎么样，我顺便买两杯奶茶回来。"
    FRESH = "是呀，不过我更想在家窝着把昨天那集剧看完。"

    @staticmethod
    def _engine_with_history(old_text: str):
        engine = _build_engine()
        engine.context_manager.get_context = AsyncMock(
            return_value=(_make_history(old_text), "")
        )
        return engine

    @staticmethod
    def _provider(repeat_text: str, fresh_text: str):
        provider = MagicMock()
        provider.model_name = "test-model"

        async def stream_reply(**kwargs: Any) -> AsyncIterator[str]:
            kwargs["usage_sink"].update(prompt_tokens=10, completion_tokens=20)
            for i in range(0, len(repeat_text), 6):
                yield repeat_text[i : i + 6]

        provider.stream_complete = MagicMock(side_effect=stream_reply)
        provider.complete_with_usage = AsyncMock(
            return_value=(fresh_text, LLMUsage(5, 7))
        )
        return provider

    def _collect(self, engine, provider, text: str) -> list[Any]:
        async def flow() -> list[Any]:
            agen = engine.handle_message_stream(
                platform="qq", platform_id="openid-1", text=text
            )
            try:
                return [item async for item in agen]
            finally:
                await agen.aclose()

        # 一次性叠好所有替身：在循环里 return 只会生效第一个 flag
        with ExitStack() as stack:
            stack.enter_context(
                patch("app.conversation.engine.get_llm_provider", return_value=provider)
            )
            stack.enter_context(
                patch(
                    "app.conversation.engine.spawn_with_limit",
                    side_effect=_discard_background,
                )
            )
            for flag in (
                "function_calling_enabled", "emotion_llm_fallback_enabled",
                "support_strategy_enabled", "consistency_check_enabled",
                "character_mood_enabled", "sticker_enabled", "embedding_enabled",
            ):
                stack.enter_context(patch.object(settings, flag, False))
            return asyncio.run(flow())

    def test_prefix_repeat_triggers_reset_and_retry(self) -> None:
        engine = self._engine_with_history(self.OLD)
        provider = self._provider(self.OLD, self.FRESH)
        with patch.object(settings, "stream_gate_enabled", True), patch.object(
            settings, "stream_jitter_guard_enabled", True
        ):
            items = self._collect(engine, provider, "周末干嘛")
        kinds = [getattr(i, "kind", "text") for i in items]
        assert "reset" in kinds
        provider.complete_with_usage.assert_awaited_once()
        tail = "".join(
            getattr(i, "text", "")
            for i in items[kinds.index("reset") + 1 :]
            if getattr(i, "kind", "") == "msg"
        )
        # 收回之后的新内容必须来自重跑，而不是那段照抄的旧回复
        assert "是呀" in tail and self.OLD not in tail

    def test_stored_text_is_the_retry_not_the_repeat(self) -> None:
        engine = self._engine_with_history(self.OLD)
        provider = self._provider(self.OLD, self.FRESH)
        with patch.object(settings, "stream_gate_enabled", True), patch.object(
            settings, "stream_jitter_guard_enabled", True
        ):
            self._collect(engine, provider, "周末干嘛")
        stored = engine.turn_recorder.record_assistant_message.await_args.kwargs["clean_text"]
        assert self.OLD not in stored
        assert "是呀" in stored

    def test_retry_usage_billed(self) -> None:
        engine = self._engine_with_history(self.OLD)
        provider = self._provider(self.OLD, self.FRESH)
        with patch.object(settings, "stream_gate_enabled", True), patch.object(
            settings, "stream_jitter_guard_enabled", True
        ):
            self._collect(engine, provider, "周末干嘛")
        # 流式 10+20 与重跑 5+7 都要计入，重跑的真实成本不能被漏记
        engine.quota_repo.charge.assert_awaited_once()
        assert 42 in engine.quota_repo.charge.await_args.args

    def test_guard_off_no_retry(self) -> None:
        engine = self._engine_with_history(self.OLD)
        provider = self._provider(self.OLD, self.FRESH)
        with patch.object(settings, "stream_gate_enabled", True), patch.object(
            settings, "stream_jitter_guard_enabled", False
        ):
            items = self._collect(engine, provider, "周末干嘛")
        assert "reset" not in [getattr(i, "kind", "text") for i in items]
        provider.complete_with_usage.assert_not_awaited()

    def test_reset_max_zero_disables_recovery(self) -> None:
        engine = self._engine_with_history(self.OLD)
        provider = self._provider(self.OLD, self.FRESH)
        with patch.object(settings, "stream_gate_enabled", True), patch.object(
            settings, "stream_jitter_guard_enabled", True
        ), patch.object(settings, "stream_jitter_reset_max", 0):
            items = self._collect(engine, provider, "周末干嘛")
        assert "reset" not in [getattr(i, "kind", "text") for i in items]
        provider.complete_with_usage.assert_not_awaited()

    def test_gate_off_guard_is_inert(self) -> None:
        """闸门关闭时护栏必须完全不起作用：输出仍要与改动前逐字节一致。"""
        engine = self._engine_with_history(self.OLD)
        provider = self._provider(self.OLD, self.FRESH)
        with patch.object(settings, "stream_jitter_guard_enabled", True):
            items = self._collect(engine, provider, "周末干嘛")
        assert all(isinstance(i, str) for i in items)
        provider.complete_with_usage.assert_not_awaited()

    def test_overlap_signal_catches_reordered_repeat(self) -> None:
        """前缀尺子看不见的换序复述，覆盖度尺子必须能看见。

        测试服实测：同一句"我最近睡不好"连问四轮，第四轮把第三轮整句搬到中间，
        公共前缀不到 16 字、difflib 只有 0.67，旧的两把尺子全漏，护栏形同虚设。
        """
        engine = _build_engine()
        old = (
            "呜呜学长睡不好 人家听着好心疼 是不是最近考研复习太满，脑子停不下来呀？"
            " 你试试今晚睡前泡个热水脚嘛，暖乎乎的，再把手机放远一点点，小口喝杯温牛奶"
        )
        repeated = (
            "学长是不是压力太大了呀，考研复习脑子停不下来 人家听着都心疼了啦，"
            "你试试今晚睡前泡个热水脚嘛，暖乎乎的，再把手机放远一点点，小口喝杯温牛奶"
        )
        variation = (
            "学长也别太急啦，睡前少刷会儿手机会不会好一些？"
            " 要不要明天早上补个短觉，白天就不会一直飘着"
        )
        with patch.object(settings, "stream_jitter_precheck_chars", 24):
            assert engine._looks_like_stream_repeat(repeated, [old]) is True
            assert engine._looks_like_stream_repeat(variation, [old]) is False

    def test_precheck_needs_enough_text_first(self) -> None:
        engine = _build_engine()
        old = "白天整个人都飘乎乎的人家听着都心疼要不今晚试试把手机放远一点点"
        with patch.object(settings, "stream_jitter_precheck_chars", 24):
            assert engine._looks_like_stream_repeat(old[:10], [old]) is False
            assert engine._looks_like_stream_repeat(old, []) is False

    def test_retry_failure_falls_back_without_blank_screen(self) -> None:
        """重跑失败：收回复读后必须还给用户一句话，不能只剩一个被清空的回合。"""
        engine = self._engine_with_history(self.OLD)
        provider = self._provider(self.OLD, self.FRESH)
        provider.complete_with_usage = AsyncMock(side_effect=RuntimeError("上游炸了"))
        with patch.object(settings, "stream_gate_enabled", True), patch.object(
            settings, "stream_jitter_guard_enabled", True
        ):
            items = self._collect(engine, provider, "周末干嘛")
        kinds = [getattr(i, "kind", "text") for i in items]
        assert "reset" in kinds
        after_reset = kinds.index("reset")
        tail_texts = [
            getattr(i, "text", "")
            for i in items[after_reset + 1 :]
            if getattr(i, "kind", "") == "msg"
        ]
        # 复读内容不许在收回后重新出现，屏幕上只剩兜底句
        assert tail_texts and self.OLD not in "".join(tail_texts)
        stored = engine.turn_recorder.record_assistant_message.await_args.kwargs["clean_text"]
        assert self.OLD not in stored and stored


class TestEmptyReplyFallback:
    """整轮被清洗成空时的兜底。

    此前非流式链路把空串一路带到适配器，QQ 以 400/40034030「content 不能为空」拒绝，
    失败只落日志——对方视角就是已读不回。流式链路早有同款兜底，本类补齐非流式。
    """

    def test_empty_llm_output_gets_fallback_sentence(self) -> None:
        """模型输出为空：本轮文本落到兜底句，且落库与所见是同一份。"""
        from app.conversation.engine import EMPTY_REPLY_FALLBACK

        engine = _build_engine()
        with patch.object(settings, "empty_reply_fallback_enabled", True):
            result, _ = _run(engine, "今天好累", reply="")
        assert result.text == EMPTY_REPLY_FALLBACK
        assert result.texts == [EMPTY_REPLY_FALLBACK]
        stored = engine.turn_recorder.record_assistant_message.await_args.kwargs["clean_text"]
        assert stored == EMPTY_REPLY_FALLBACK

    def test_disabled_switch_keeps_empty(self) -> None:
        """关掉开关即回退旧行为：本轮没有文本可发（由适配器负责不发出去）。"""
        engine = _build_engine()
        with patch.object(settings, "empty_reply_fallback_enabled", False):
            result, _ = _run(engine, "今天好累", reply="")
        assert result.text == ""
        assert not [t for t in result.texts if t and t.strip()]

    def test_sticker_only_turn_not_polluted(self) -> None:
        """只发表情包的一轮不该被塞进兜底文字：有可视内容时保持原样。"""
        engine = _build_engine()
        with patch.object(settings, "empty_reply_fallback_enabled", True), patch.object(
            engine, "_resolve_sticker_tasks",
            AsyncMock(return_value=[SimpleNamespace(name="smile")]),
        ):
            result, _ = _run(engine, "发个表情", reply="")
        assert result.text == ""
        assert result.sticker_tasks


def _stream_text(chunks: list) -> str:
    """把流式产出归一成纯文本：闸门开启是 StreamEvent，关闭是裸 str。"""
    out = []
    for item in chunks:
        out.append(getattr(item, "text", item) if not hasattr(item, "kind") else item.text)
    return "".join(out)


class TestStreamDisplayCleaning:
    """流式下发与落库口径一致：内部标记不给浏览器，但仍要参与收尾解析。"""

    def test_markup_never_reaches_the_client(self) -> None:
        """`[IMG:]` / `[表情:]` / 动作标签不许出现在下发内容里。

        顺带钉住一个事实：流式链路只产文本——`_resolve_stream_text` 把 specs 丢弃，
        既不建图片任务也不扣图片额度（非流式才会）。所以网页端看到纯文本气泡是正确的，
        不该出现半截标签；真要流式出图得另做协议，不属本用例范围。"""

        engine = _build_engine(profile=_make_profile(
            reference_image_url="/api/media/ref1",
            persona=SimpleNamespace(appearance="a young woman"),
        ))
        old_imagegen = settings.imagegen_enabled
        old_gate = settings.stream_gate_enabled
        settings.imagegen_enabled = True
        settings.stream_gate_enabled = False
        try:
            chunks, _provider = _run(
                engine,
                "拍一张",
                reply="（微笑）给你拍一张[IMG:realistic photo, sunset over the sea]",
                stream=True,
            )
        finally:
            settings.imagegen_enabled = old_imagegen
            settings.stream_gate_enabled = old_gate
        joined = _stream_text(chunks)
        assert "[IMG:" not in joined and "realistic photo" not in joined, joined
        assert "微笑" not in joined, joined
        assert "给你拍一张" in joined, joined
        # 落库文本同样是清洗后的（屏幕所见即落库所持）
        persisted = str(engine.turn_recorder.record_assistant_message.await_args)
        assert "[IMG:" not in persisted, persisted
        assert "realistic photo" not in persisted, persisted

    def test_stream_with_gate_enabled_is_also_clean(self) -> None:
        """闸门开启那条分支同样要清洗：两条分支的屏幕口径必须一致。"""
        engine = _build_engine(profile=_make_profile(
            persona=SimpleNamespace(appearance="a young woman"),
        ))
        old_gate = settings.stream_gate_enabled
        settings.stream_gate_enabled = True
        try:
            chunks, _provider = _run(
                engine,
                "说点什么",
                reply="（点头）好的[表情:开心]，我在听",
                stream=True,
            )
        finally:
            settings.stream_gate_enabled = old_gate
        joined = _stream_text(chunks)
        assert "[表情:" not in joined and "开心" not in joined, joined


class TestStageTimings:
    """分阶段耗时日志：让"整轮很慢但 LLM 不慢"这类问题留下一行可读证据。

    2026-10 测试服实测出现过整轮 80s+ 而 latency_ms 只有 9s 的轮次，当时没有
    任何分阶段日志可归因，只能逐个环节猜。
    """

    def test_stream_emits_stage_summary_line(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        engine = _build_engine()
        with caplog.at_level("INFO", logger="app.conversation.engine"):
            _run(engine, "今天想出门走走", stream=True)
        line = next(
            (r.getMessage() for r in caplog.records if "流式分阶段耗时" in r.getMessage()),
            "",
        )
        assert line, [r.getMessage() for r in caplog.records]
        for key in ("resolve=", "context=", "recall=", "llm=", "pre_llm="):
            assert key in line, line

    def test_format_marks_pre_llm_sum(self) -> None:
        from app.conversation.engine import ConversationEngine

        stage = {
            "resolve": 5, "context": 7, "emotion": 1, "recall": 8000,
            "prompt": 3, "llm": 900, "media": 20,
        }
        text = ConversationEngine._format_stage_timings(stage)
        assert "recall=8000" in text
        # pre_llm 只累计发首 token 之前的阶段，不含 llm/media
        assert "pre_llm=8016" in text
        assert "total=8936" in text


