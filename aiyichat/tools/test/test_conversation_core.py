"""对话核心模块与 LLM 层测试。

覆盖：
- emotion_detector：关键词检测/否定/强度/演变/LLM 降级
- focus_tracker：提取解析/分类/冲突消解/事件（mock LLM + mongomock）
- context_manager：窗口/token 预算/增量摘要（mock LLM + mongomock）
- 引擎角色解析 / 长度软引导 / topic_manager
- openai_provider / provider_factory
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import mongomock_motor
import pytest

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

    def test_intensity(self) -> None:
        from app.conversation.emotion_detector import _compute_intensity

        # 高强度词
        assert _compute_intensity("我想死了", 1) == 1.0
        # 低强度前缀降权
        low = _compute_intensity("有点难过", 1)
        assert low < 0.5

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
    """长度引导：约束的是**条数与一句一意**，不是字数——模型数不准自己写了多少字。"""

    def test_base_guide(self) -> None:
        from app.conversation.engine import ConversationEngine

        guide = ConversationEngine._length_instruction_for("standard")
        # 条数主导 + 多消息分隔符 + 禁清单体
        assert "最多两条" in guide
        assert "|||" in guide
        assert "不列点" in guide
        # 不再包含字数硬限制类指令
        assert "字以内" not in guide
        assert "1句话即可" not in guide

    def test_verbosity_variants(self) -> None:
        from app.conversation.engine import ConversationEngine

        concise = ConversationEngine._length_instruction_for("concise")
        verbose = ConversationEngine._length_instruction_for("verbose")
        assert "话少" in concise
        assert "小作文" in verbose
        # standard 不再"什么都不写"：实测整轮中位 99 字正是从这个默认档长出来的
        assert "一到两条" in ConversationEngine._length_instruction_for("standard")
        # 未知风格回退基础文案（与 standard 一致）
        assert (
            ConversationEngine._length_instruction_for("bogus")
            == ConversationEngine._length_instruction_for("standard")
        )

    def test_mirror_follows_counterpart_length(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.config import settings
        from app.conversation import engine

        # 显式打开：镜像开关是全局配置，随机测试顺序下不能依赖别的用例没关掉它
        monkeypatch.setattr(settings, "reply_length_mirror_enabled", True)
        assert engine._mirror_hint("在吗") == engine._MIRROR_TIERS[0][1]
        assert "把话头留给对方" in engine._mirror_hint("今天真是累得不行了呀")
        assert "值得你多回一两句" in engine._mirror_hint(" x" * 60)

    def test_mirror_can_be_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.config import settings
        from app.conversation import engine
        from app.conversation.engine import ConversationEngine

        monkeypatch.setattr(settings, "reply_length_mirror_enabled", False)
        assert engine._mirror_hint("在吗") == ""
        assert "别铺成长段" not in ConversationEngine._length_instruction_for(
            "standard", "在吗"
        )


class TestReplyWarmth:
    """短而有味：长度收住之后，报喜/倾诉那一句仍要带情绪反应，不能塌成一条通知。

    实测教训（13 用例改前/改后对照）：整轮字数 76→18 的同时 emoji 命中率 11/13→6/13，
    "我升职了！！！"被回成"太好了，这段时间的辛苦总算没白费"，裁判点名"像普通短信"。
    """

    @pytest.fixture(autouse=True)
    def _warmth_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.config import settings

        monkeypatch.setattr(settings, "reply_warmth_enabled", True)

    def test_celebration_detected_without_emotion_tag(self) -> None:
        from app.conversation.engine import _warmth_instruction

        # "我升职了！！！"压根不在情绪词表里，且流式链路不做 LLM 情绪降级：
        # 靠连跑感叹号与报喜动词兜底，否则这条最典型的用例永远走不到味道分支
        assert "报喜" in _warmth_instruction("", "我升职了！！！")
        assert "报喜" in _warmth_instruction("", "上岸了 我终于上岸了")
        assert "报喜" in _warmth_instruction("happy", "今天心情特别好")

    def test_vent_detected(self) -> None:
        from app.conversation.engine import _warmth_instruction

        assert "倾诉" in _warmth_instruction("", "今天又一个人吃饭，好难受")
        assert "倾诉" in _warmth_instruction("sad", "心情不太好")

    def test_neutral_message_gets_no_warmth(self) -> None:
        from app.conversation.engine import _warmth_instruction

        # 中性消息不注入：否则等于给每条都挂表情，腻成另一种模板
        assert _warmth_instruction("", "晚上不知道吃啥，你有推荐吗") == ""

    def test_celebration_wins_over_vent(self) -> None:
        from app.conversation.engine import _warmth_instruction

        assert "报喜" in _warmth_instruction("", "熬了三个月终于升职！！！好累")

    def test_switch_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.config import settings
        from app.conversation.engine import _warmth_instruction

        monkeypatch.setattr(settings, "reply_warmth_enabled", False)
        assert _warmth_instruction("happy", "我升职了！！！") == ""

    def test_warmth_does_not_relax_length_or_bubbles(self) -> None:
        from app.conversation.engine import ConversationEngine

        guide = ConversationEngine._length_instruction_for(
            "standard", "我升职了！！！", "happy"
        )
        assert "报喜" in guide
        # 条数与一句一意的约束必须原样保留，否则长度治理的成果被这轮吃掉
        assert "最多两条" in guide
        assert "|||" in guide
        assert "不列点" in guide

    def test_emotion_tag_is_optional_for_callers(self) -> None:
        from app.conversation.engine import ConversationEngine

        # 既有调用方（含评测脚本）只传两参：新增形参必须带默认值，不能破坏签名
        assert ConversationEngine._length_instruction_for("standard", "在吗")


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
                    "new_facts": ["身份在准备考研", "喜欢喝咖啡"],
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


class TestTurnRecorder:
    """轮次记录器应保留接收时间并返回真实的落库消息对象。"""

    @pytest.mark.parametrize("explicit_time", [False, True])
    def test_record_messages_return_persisted_models(self, explicit_time: bool) -> None:
        """直接使用内存仓库验证时间透传、返回值、媒体字段及消息计数。"""
        from app.conversation.turn_recorder import TurnRecorder
        from app.db.repositories.conversation_repo import ConversationRepository
        from app.db.repositories.message_repo import MessageRepository
        from app.models.message import MessageInDB
        from tools.test.db_test_utils import run_with_db

        async def body() -> None:
            """模拟 LLM 完成后才落库，身份时间仍为接收时刻。"""
            conversations = ConversationRepository()
            messages = MessageRepository()
            conversation = await conversations.create("u-1", "p-1", "svc-1")
            recorder = TurnRecorder(messages, conversations)
            written_at = datetime(2026, 10, 15, 8, tzinfo=timezone.utc)
            received_at = written_at - timedelta(minutes=2)
            kwargs = {"created_at": received_at} if explicit_time else {}
            with patch("app.db.repositories.message_repo.datetime", wraps=datetime) as clock:
                clock.now.return_value = written_at
                identity_msg = await recorder.record_identity_message(
                    conversation.id, "今天很开心", "happy",
                    media_url="/api/media/test-image", content_type="image", **kwargs,
                )
                assistant_msg = await recorder.record_assistant_message(
                    conversation.id, "替你开心", "test-model", 120,
                )
            assert isinstance(identity_msg, MessageInDB)
            assert isinstance(assistant_msg, MessageInDB)
            assert identity_msg.id != assistant_msg.id
            assert identity_msg.role == "user" and identity_msg.content == "今天很开心"
            assert identity_msg.emotion_tag == "happy"
            assert identity_msg.media_url == "/api/media/test-image"
            assert identity_msg.content_type == "image"
            assert identity_msg.created_at == (received_at if explicit_time else written_at)
            assert identity_msg.updated_at == written_at
            assert assistant_msg.role == "assistant"
            assert assistant_msg.content == "替你开心"
            assert assistant_msg.llm_model == "test-model"
            assert assistant_msg.latency_ms == 120
            assert assistant_msg.created_at == assistant_msg.updated_at == written_at
            stored = await messages.get_recent(conversation.id)
            assert [m.id for m in stored] == [identity_msg.id, assistant_msg.id]
            await recorder.update_conversation_meta(conversation.id)
            updated = await conversations.find_by_id(conversation.id)
            assert updated is not None and updated.message_count == 2

        run_with_db(body)


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
