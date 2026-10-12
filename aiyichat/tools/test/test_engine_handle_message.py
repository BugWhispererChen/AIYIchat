"""ConversationEngine.handle_message 核心编排路径行为测试。

用 mock 隔离数据库/网络（模式同 test_bugfixes.py），聚焦主链路不变量：
- 正常路径：Bot 校验→用户/服务解析→额度预检→prompt 组装→LLM→usage 结算→持久化
- 拒绝/降级路径：Bot 不存在、余额不足预检拦截、危机干预拦截、LLM 失败不扣费
- Token 计费：结算金额=API usage 总量、预检仅查不扣、usage 缺失跳过结算

安全检查器/情绪检测/回复清洗为纯规则实现，走真实代码；
仓储层、上下文管理、prompt 组装、LLM Provider、后台任务均为 mock。
"""

import asyncio
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import settings
from app.llm.base import LLMUsage


def _make_bot(**overrides):
    base = dict(id="bot-1", account_id="acc-1", is_blocked=False, block_reason="")
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_user(**overrides):
    base = dict(
        id="user-1", is_blocked=False, block_reason="",
        is_minor=False, platform_id="real-openid",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_service(**overrides):
    base = dict(
        id="svc-1", user_id="user-1", account_id="acc-1",
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
    bot=..., user=..., service=..., profile=..., quota_doc=...,
):
    """构建各依赖均被 mock 的引擎实例（省略参数使用默认正常数据）。"""
    from app.conversation.engine import ConversationEngine

    engine = ConversationEngine("bot-1")

    engine.bot_repo = MagicMock()
    engine.bot_repo.find_by_id = AsyncMock(
        return_value=_make_bot() if bot is ... else bot
    )

    engine.user_repo = MagicMock()
    engine.user_repo.find_by_platform = AsyncMock(
        return_value=_make_user() if user is ... else user
    )
    engine.user_repo.update_last_active = AsyncMock()
    # 按 Bot 记录 openid 映射（主动消息路由），主链路不关心其返回值
    engine.user_repo.bind_bot_openid = AsyncMock(return_value=False)

    engine.service_repo = MagicMock()
    engine.service_repo.find_by_user_and_bot = AsyncMock(
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
    engine.quota_repo.refund = AsyncMock(return_value=True)

    engine.turn_recorder = MagicMock()
    engine.turn_recorder.record_user_message = AsyncMock()
    engine.turn_recorder.record_assistant_message = AsyncMock()
    engine.turn_recorder.update_conversation_meta = AsyncMock()

    engine.memory_repo = MagicMock()
    engine.memory_repo.find_by_service = AsyncMock(return_value=None)

    engine.safety_repo = MagicMock()
    engine.safety_repo.create = AsyncMock()

    engine.context_manager = MagicMock()
    engine.context_manager.get_context = AsyncMock(return_value=([], ""))

    engine.prompt_assembler = MagicMock()
    engine.prompt_assembler.assemble = MagicMock(
        return_value=[
            {"role": "system", "content": "persona"},
            {"role": "user", "content": "input"},
        ]
    )

    engine.focus_tracker = MagicMock()
    engine.focus_tracker.should_extract = MagicMock(return_value=False)

    return engine


def _run(
    engine,
    text,
    reply="你好呀，今天过得怎么样？",
    usage=None,
    llm_error=None,
    replies=None,
):
    """在收窄开关 + 隔离 LLM/后台任务的环境下执行一轮 handle_message。

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

    with ExitStack() as stack:
        stack.enter_context(
            patch("app.conversation.engine.get_llm_provider", return_value=provider)
        )
        stack.enter_context(patch("app.conversation.engine.spawn"))
        for flag in (
            "function_calling_enabled",
            "emotion_llm_fallback_enabled",
            "support_strategy_enabled",
            "consistency_check_enabled",
            "character_mood_enabled",
        ):
            stack.enter_context(patch.object(settings, flag, False))
        result = asyncio.run(
            engine.handle_message(platform="qq", platform_id="openid-1", text=text)
        )
    return result, provider


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

        # Token 结算：仅一次扣减，金额 = API usage 总量（非字符数）
        engine.quota_repo.deduct.assert_awaited_once_with(
            "acc-1", "text", usage.total
        )

        # 双向消息均持久化，且 AI 回复带模型名
        engine.turn_recorder.record_user_message.assert_awaited_once()
        assert (
            engine.turn_recorder.record_user_message.await_args
            .kwargs["text"] == text
        )
        engine.turn_recorder.record_assistant_message.assert_awaited_once()
        assistant_kwargs = (
            engine.turn_recorder.record_assistant_message.await_args.kwargs
        )
        assert assistant_kwargs["clean_text"] == reply
        assert assistant_kwargs["llm_model"] == "test-model"

        # 会话元数据与用户活跃时间已刷新
        engine.turn_recorder.update_conversation_meta.assert_awaited_once_with(
            "conv-1"
        )
        engine.user_repo.update_last_active.assert_awaited_once_with("user-1")

        # 按 Bot 记录 openid 映射（主动消息需按 Bot 路由发送目标）
        engine.user_repo.bind_bot_openid.assert_awaited_once_with(
            "user-1", "bot-1", "openid-1"
        )


class TestAutoBindOpenid:
    """未绑定用户自动绑定分支：platform_id 与 Bot 维度映射同步写入。"""

    def test_bind_platform_id_and_bot_openid(self) -> None:
        engine = _build_engine(user=None)
        # 预创建用户（platform_id 为 UUID = 未绑定），服务按 Bot 反查
        engine.user_repo.find_by_id = AsyncMock(
            return_value=_make_user(
                platform_id="12345678-1234-1234-1234-123456789abc"
            )
        )
        engine.user_repo.bind_platform_id = AsyncMock()
        engine.service_repo.find_active_by_bot = AsyncMock(
            return_value=_make_service()
        )

        _run(engine, "你好")

        engine.user_repo.bind_platform_id.assert_awaited_once_with(
            "user-1", "openid-1", ""
        )
        engine.user_repo.bind_bot_openid.assert_awaited_once_with(
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
        engine.turn_recorder.record_user_message.assert_not_awaited()
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
        engine.turn_recorder.record_user_message.assert_not_awaited()
        engine.turn_recorder.record_assistant_message.assert_not_awaited()


class TestTokenBilling:
    """Token 计费语义：结算金额=usage 总量、预检拦截、零 usage 跳过。"""

    def test_settle_amount_equals_usage_total(self) -> None:
        """结算扣费金额必须等于 prompt+completion 总量。"""
        engine = _build_engine()
        usage = LLMUsage(prompt_tokens=128, completion_tokens=64)

        _run(engine, "聊聊最近的事", usage=usage)

        engine.quota_repo.deduct.assert_awaited_once_with(
            "acc-1", "text", 192
        )

    def test_precheck_threshold_blocks_llm(self) -> None:
        """余额低于 预估输入 token + 输出上限 → 拒绝且不调用 LLM。"""
        # 余额 2050 < estimate_tokens("你好")(3) + chat_max_tokens(2048) = 2051
        engine = _build_engine(quota_doc=_make_quota_doc(limit=4098, used=2048))

        result, provider = _run(engine, "你好")

        assert result.text.startswith("额度不足")
        provider.complete_with_usage.assert_not_awaited()
        engine.quota_repo.deduct.assert_not_awaited()

    def test_zero_usage_skips_deduct(self) -> None:
        """后端未返回 usage（零值）：跳过扣费，回复照常返回。"""
        engine = _build_engine()

        result, _ = _run(
            engine, "在吗", usage=LLMUsage(0, 0), reply="在的呀"
        )

        assert result.text == "在的呀"
        engine.quota_repo.deduct.assert_not_awaited()


class TestBotNotFound:
    """Bot 不存在：最先拒绝，不触发任何后续查询。"""

    def test_rejects_before_user_lookup(self) -> None:
        engine = _build_engine(bot=None)

        result, provider = _run(engine, "在吗")

        assert result.text == "您尚未绑定对话服务，请联系管理员。"
        assert result.conversation_id == ""
        engine.user_repo.find_by_platform.assert_not_awaited()
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
            role="user", content="用户消息", emotion_tag=None,
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
        engine.quota_repo.deduct.assert_awaited_once_with("acc-1", "text", 40)

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
        engine.quota_repo.deduct.assert_awaited_once()

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

