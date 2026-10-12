"""对话核心模块与 LLM 层测试。

覆盖：
- emotion_detector：关键词检测/否定/强度/演变/LLM 降级
- focus_tracker：提取解析/分类/冲突消解/事件（mock LLM + mongomock）
- context_manager：窗口/token 预算/增量摘要（mock LLM + mongomock）
- profile_resolver / 长度软引导 / topic_manager
- openai_provider / provider_factory
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import mongomock_motor

from app.config import settings
from app.db import mongo


def _run(coro):
    return asyncio.run(coro)


class TestEmotionDetector:
    """情绪检测。"""

    def test_detect_basic(self) -> None:
        from app.conversation.emotion_detector import detect_emotion

        assert detect_emotion("我今天好难过想哭") == "sad"
        assert detect_emotion("太好了我很开心") == "happy"
        assert detect_emotion("今天天气不错") is None

    def test_negation_filter(self) -> None:
        from app.conversation.emotion_detector import _detect_core

        # "不难过" 被否定过滤 → 无情绪
        assert _detect_core("我一点也不难过") is None

    def test_intensity_and_hint(self) -> None:
        from app.conversation.emotion_detector import (
            EmotionResult,
            _compute_intensity,
            get_emotion_hint,
        )

        # 高强度词
        assert _compute_intensity("我想死了", 1) == 1.0
        # 低强度前缀降权
        low = _compute_intensity("有点难过", 1)
        assert low < 0.5

        # get_emotion_hint 三种入参
        assert get_emotion_hint(None) is None
        assert get_emotion_hint("sad", 0.9)
        er = EmotionResult(emotion="happy", intensity=0.6, hint="开心")
        assert get_emotion_hint(er) == "开心"

    def test_detect_with_history_evolution(self) -> None:
        from app.conversation.emotion_detector import detect_emotion_with_history

        # 持续低落
        r = detect_emotion_with_history("还是很难过", ["sad", "sad", "sad"])
        assert r is not None and "持续低落" in r.hint
        # 好转
        r = detect_emotion_with_history("今天很开心", ["sad", "sad"])
        assert r is not None and "好转" in r.hint
        # 变差
        r = detect_emotion_with_history("突然好难过", ["happy"])
        assert r is not None and "变差" in r.hint
        # 无历史
        r = detect_emotion_with_history("好难过", [])
        assert r is not None
        # 无情绪
        assert detect_emotion_with_history("普通对话", ["sad"]) is None

    def test_detect_emotion_llm(self) -> None:
        async def body():
            from app.conversation import emotion_detector

            fake_provider = SimpleNamespace(complete=AsyncMock(return_value=json.dumps({
                "emotion": "sad", "intensity": 0.6, "hint": "有些低落",
            })))
            with patch(
                "app.llm.provider_factory.get_llm_provider",
                return_value=fake_provider,
            ):
                r = await emotion_detector.detect_emotion_llm("又加班到十点")
                assert r is not None and r.emotion == "sad"

            # 空文本 → None
            assert await emotion_detector.detect_emotion_llm("") is None

            # 中性情绪 → None
            fake_provider.complete = AsyncMock(return_value=json.dumps({
                "emotion": "neutral", "intensity": 0.5,
            }))
            with patch(
                "app.llm.provider_factory.get_llm_provider",
                return_value=fake_provider,
            ):
                assert await emotion_detector.detect_emotion_llm("嗯") is None

        _run(body())


class TestLengthSoftGuide:
    """回复长度软引导：不设字数硬限制，仅按角色风格做文案软引导。"""

    def test_base_guide(self) -> None:
        from app.conversation.engine import ConversationEngine

        guide = ConversationEngine._length_instruction_for("standard")
        # 软引导包含情景自由决定长度与多消息分隔符引导
        assert "自由决定" in guide
        assert "|||" in guide
        # 不再包含字数硬限制类指令
        assert "字以内" not in guide
        assert "1句话即可" not in guide

    def test_verbosity_variants(self) -> None:
        from app.conversation.engine import ConversationEngine

        concise = ConversationEngine._length_instruction_for("concise")
        verbose = ConversationEngine._length_instruction_for("verbose")
        assert "凝练" in concise
        assert "健谈" in verbose
        # 未知风格回退基础文案（与 standard 一致）
        assert (
            ConversationEngine._length_instruction_for("bogus")
            == ConversationEngine._length_instruction_for("standard")
        )


class TestTopicManager:
    """话题管理。"""

    def test_detect_and_context(self) -> None:
        from app.conversation.topic_manager import TopicManager

        tm = TopicManager()
        assert tm.detect_topic_shift("对了我想问", "旧话题") is True
        assert tm.detect_topic_shift("继续聊", "旧话题") is False
        assert tm.generate_topic_context([], "") == ""
        assert tm.generate_topic_context([], "当前") == ""
        ctx = tm.generate_topic_context(["A", "B", "C", "D"], "E")
        assert "当前：E" in ctx and "B→C→D" in ctx
        ctx2 = tm.generate_topic_context(["A", "B"], "")
        assert "A→B" in ctx2


class TestProfileResolver:
    """角色解析。"""

    def test_resolve_priorities(self) -> None:
        async def body():
            from app.conversation.profile_resolver import ProfileResolver

            locked = SimpleNamespace(slug="locked")
            active = SimpleNamespace(is_active=True, id="p-1")
            inactive = SimpleNamespace(is_active=False, id="p-2")

            repo = SimpleNamespace(
                find_by_slug=AsyncMock(return_value=locked),
                find_by_id=AsyncMock(return_value=active),
            )
            # 锁定优先
            r = ProfileResolver(repo, locked_profile_slug="locked")
            assert await r.resolve("p-1") is locked

            # 无锁定 + active profile_id
            r2 = ProfileResolver(repo)
            assert await r2.resolve("p-1") is active

            # profile_id 非活跃 → 返回 None（不再默认兜底）
            repo.find_by_id = AsyncMock(return_value=inactive)
            r3 = ProfileResolver(repo)
            assert await r3.resolve("p-2") is None

            # 无 profile_id → 返回 None，提示文案引导创建角色
            assert await r3.resolve(None) is None
            assert "创建角色" in r3.no_profile_message()

        _run(body())


class TestFocusTracker:
    """焦点追踪。"""

    def test_should_extract(self) -> None:
        from app.conversation.focus_tracker import FocusTracker

        ft = FocusTracker()
        old = settings.memory_extract_interval
        settings.memory_extract_interval = 6
        try:
            assert ft.should_extract(6, "普通消息") is True
            assert ft.should_extract(5, "普通消息") is False
            # 话题转换信号提前触发
            assert ft.should_extract(5, "对了还有件事") is True
            # 5分钟内不重复
            from datetime import datetime, timezone
            assert ft.should_extract(6, "x", datetime.now(timezone.utc)) is False
            # naive datetime（MongoDB 读出无时区）不抛异常，按 UTC 归一
            assert ft.should_extract(6, "x", datetime.utcnow()) is False
        finally:
            settings.memory_extract_interval = old

    def test_classify_and_parse(self) -> None:
        from app.conversation.focus_tracker import FocusTracker

        assert FocusTracker._classify_fact("喜欢猫") == "preference"
        assert FocusTracker._classify_fact("有个朋友") == "relationship"
        assert FocusTracker._classify_fact("压力很大") == "mood_pattern"
        assert FocusTracker._classify_fact("准备考研") == "life_event"
        assert FocusTracker._classify_fact("普通事实") == "general"

        # JSON 解析（含 markdown 包裹）
        assert FocusTracker._parse_response('```json\n{"a":1}\n```') == {"a": 1}
        assert FocusTracker._parse_response("not-json") is None

        # 字符相似度
        assert FocusTracker._char_similarity("abc", "abc") == 1.0
        assert FocusTracker._char_similarity("", "x") == 0.0

    def test_extract_and_save(self) -> None:
        async def body():
            mongo._db = mongomock_motor.AsyncMongoMockClient()["test_focus"]
            try:
                from app.conversation.focus_tracker import FocusTracker
                from app.models.message import MessageInDB

                ft = FocusTracker()
                fake_provider = SimpleNamespace(complete=AsyncMock(return_value=json.dumps({
                    "current_topic": "考研复习",
                    "new_facts": ["用户在准备考研", "喜欢喝咖啡"],
                })))
                msgs = [
                    MessageInDB(
                        _id="m1", conversation_id="c1", role="user", content="我在准备考研",
                    ),
                    MessageInDB(
                        _id="m2", conversation_id="c1", role="assistant", content="加油",
                    ),
                ]
                with patch(
                    "app.conversation.focus_tracker.get_llm_provider",
                    return_value=fake_provider,
                ):
                    await ft.extract_and_save("svc-1", msgs, [])
                mem = await ft.memory_repo.find_by_service("svc-1")
                assert mem is not None and mem.current_topic == "考研复习"
                assert len(mem.facts) == 2
            finally:
                mongo._db = None

        _run(body())

    def test_extract_empty_messages(self) -> None:
        async def body():
            from app.conversation.focus_tracker import FocusTracker

            ft = FocusTracker()
            assert await ft._extract([], []) is None

        _run(body())

    def test_decide_action(self) -> None:
        async def body():
            from app.conversation.focus_tracker import FocusTracker

            ft = FocusTracker()
            fake_provider = SimpleNamespace(complete=AsyncMock(return_value='{"action": "UPDATE"}'))
            with patch(
                "app.conversation.focus_tracker.get_llm_provider",
                return_value=fake_provider,
            ):
                assert await ft._decide_action("旧", "新") == "UPDATE"
            # 异常兜底 ADD
            fake_provider.complete = AsyncMock(side_effect=RuntimeError("x"))
            with patch(
                "app.conversation.focus_tracker.get_llm_provider",
                return_value=fake_provider,
            ):
                assert await ft._decide_action("旧", "新") == "ADD"

        _run(body())


class TestContextManager:
    """上下文管理。"""

    def test_window_and_token_budget(self) -> None:
        from app.conversation.context_manager import ContextManager, _count_tokens

        cm = ContextManager()
        old = settings.context_window_size
        settings.context_window_size = 20
        try:
            assert cm._compute_window_size(0.0) == 20
            assert cm._compute_window_size(0.8) == 30
        finally:
            settings.context_window_size = old

        assert _count_tokens("") == 0
        assert _count_tokens("hello world") > 0

    def test_trim_to_token_budget(self) -> None:
        from app.conversation.context_manager import ContextManager
        from app.models.message import MessageInDB

        cm = ContextManager()
        old = settings.context_token_budget
        settings.context_token_budget = 5
        try:
            msgs = [
                MessageInDB(_id=str(i), conversation_id="c", role="user", content="词" * 20)
                for i in range(5)
            ]
            kept = cm._trim_to_token_budget(msgs, None)
            # 至少保留最近 1 条
            assert 1 <= len(kept) <= 5
            # 预算<=0 不裁剪
            settings.context_token_budget = 0
            assert len(cm._trim_to_token_budget(msgs, None)) == 5
        finally:
            settings.context_token_budget = old

    def test_get_context(self) -> None:
        async def body():
            mongo._db = mongomock_motor.AsyncMongoMockClient()["test_ctx"]
            try:
                from app.conversation.context_manager import ContextManager
                from app.db.repositories.message_repo import MessageRepository

                repo = MessageRepository()
                await repo.create("c-1", "user", "你好")
                cm = ContextManager()
                msgs, summary = await cm.get_context("c-1", "旧摘要")
                assert len(msgs) == 1 and summary == "旧摘要"
            finally:
                mongo._db = None

        _run(body())

    def test_schedule_summary(self) -> None:
        async def body():
            mongo._db = mongomock_motor.AsyncMongoMockClient()["test_summary"]
            try:
                from app.conversation.context_manager import ContextManager
                from app.db.repositories.conversation_repo import ConversationRepository
                from app.db.repositories.message_repo import MessageRepository

                old_trigger = settings.summary_trigger_count
                old_win = settings.context_window_size
                settings.summary_trigger_count = 3
                settings.context_window_size = 2
                try:
                    conv_repo = ConversationRepository()
                    msg_repo = MessageRepository()
                    conv = await conv_repo.create("u-1", "p-1", "svc-1")
                    for i in range(6):
                        await msg_repo.create(conv.id, "user", f"消息{i}内容较长一些")

                    fake_provider = SimpleNamespace(complete=AsyncMock(
                        return_value="这是一段足够长的对话摘要内容用于通过质量校验"
                    ))
                    cm = ContextManager()
                    with patch(
                        "app.conversation.context_manager.get_llm_provider",
                        return_value=fake_provider,
                    ):
                        await cm.schedule_summary_if_needed(conv.id, None, 0)
                    updated = await conv_repo.find_by_id(conv.id)
                    assert updated is not None and updated.summary
                finally:
                    settings.summary_trigger_count = old_trigger
                    settings.context_window_size = old_win
            finally:
                mongo._db = None

        _run(body())


class TestLLMProvider:
    """LLM Provider 与工厂。"""

    def test_complete_and_tools(self) -> None:
        async def body():
            from app.llm.openai_provider import OpenAICompatibleProvider

            provider = OpenAICompatibleProvider(
                base_url="http://x", api_key="k", model="m", timeout=30
            )
            assert provider.model_name == "m"

            fake_msg = SimpleNamespace(content="回复内容", tool_calls=None)
            fake_resp = SimpleNamespace(choices=[SimpleNamespace(message=fake_msg)])
            provider._client = SimpleNamespace(
                chat=SimpleNamespace(
                    completions=SimpleNamespace(create=AsyncMock(return_value=fake_resp))
                )
            )
            out = await provider.complete([{"role": "user", "content": "hi"}])
            assert out == "回复内容"

            # 带 response_format
            out = await provider.complete(
                [{"role": "user", "content": "hi"}],
                response_format={"type": "json_object"},
            )
            assert out == "回复内容"

            # 工具调用
            tc = SimpleNamespace(
                id="t1",
                function=SimpleNamespace(name="fn", arguments='{"a":1}'),
            )
            fake_msg2 = SimpleNamespace(content="", tool_calls=[tc])
            fake_resp2 = SimpleNamespace(choices=[SimpleNamespace(message=fake_msg2)])
            provider._client.chat.completions.create = AsyncMock(return_value=fake_resp2)
            result = await provider.complete_with_tools(
                [{"role": "user", "content": "hi"}], tools=[{"type": "function"}]
            )
            assert result.tool_calls[0].name == "fn"

        _run(body())

    def test_stream_complete(self) -> None:
        async def body():
            from app.llm.openai_provider import OpenAICompatibleProvider

            provider = OpenAICompatibleProvider(
                base_url="http://x", api_key="k", model="m", timeout=30
            )

            async def fake_stream():
                # 空 choices 块应被跳过
                yield SimpleNamespace(choices=[])
                yield SimpleNamespace(choices=[
                    SimpleNamespace(delta=SimpleNamespace(content="你"))
                ])
                yield SimpleNamespace(choices=[
                    SimpleNamespace(delta=SimpleNamespace(content="好"))
                ])

            provider._client = SimpleNamespace(
                chat=SimpleNamespace(
                    completions=SimpleNamespace(create=AsyncMock(return_value=fake_stream()))
                )
            )
            chunks = [c async for c in provider.stream_complete(
                [{"role": "user", "content": "hi"}]
            )]
            assert "".join(chunks) == "你好"

        _run(body())

    def test_provider_factory(self) -> None:
        from app.llm import provider_factory

        old_cfgs = settings.model_configs
        old_cid = settings.chat_config_id
        try:
            settings.model_configs = [{
                "id": "g1", "name": "组1", "base_url": "http://x",
                "api_key": "sk-1", "default_model": "m1",
            }]
            settings.chat_config_id = "g1"
            provider_factory.reset_provider()
            p1 = provider_factory.get_llm_provider("chat")
            p2 = provider_factory.get_llm_provider("chat")
            assert p1 is p2  # 单例缓存
            provider_factory.reset_provider()
            p3 = provider_factory.get_llm_provider("chat")
            assert p3 is not p1
        finally:
            settings.model_configs = old_cfgs
            settings.chat_config_id = old_cid
            provider_factory.reset_provider()
