import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from bson import ObjectId

from app.config import settings
from app.conversation.consistency_checker import ConsistencyChecker
from app.conversation.context_manager import ContextManager
from app.conversation.emotion_detector import (
    EmotionResult,
    detect_emotion,
    detect_emotion_llm,
    detect_emotion_with_history,
)
from app.conversation.focus_tracker import FocusTracker
from app.conversation.image_task import ImageTask
from app.conversation.message_splitter import split_messages
from app.conversation.mood_manager import MoodManager
from app.conversation.reply_jitter import normalize_text
from app.conversation.response_processor import ResponseProcessor
from app.conversation.safety_checker import SafetyAction, get_safety_checker
from app.conversation.sticker_task import StickerTask
from app.conversation.support_strategy import (
    NEGATIVE_EMOTIONS,
    SupportStrategySelector,
)
from app.conversation.topic_manager import TopicManager
from app.conversation.turn_recorder import TurnRecorder
from app.db.repositories.account_repo import AccountRepository
from app.db.repositories.bot_config_repo import BotConfigRepository
from app.db.repositories.conversation_repo import ConversationRepository
from app.db.repositories.message_repo import MessageRepository
from app.db.repositories.profile_repo import ProfileRepository
from app.db.repositories.proactive_repo import ProactiveRecordRepository
from app.db.repositories.quota_repo import QuotaRepository
from app.db.repositories.safety_event_repo import SafetyEventRepository
from app.db.repositories.service_memory_repo import ServiceMemoryRepository
from app.db.repositories.service_repo import ServiceRepository
from app.db.repositories.user_repo import UserRepository
from app.llm.base import LLMUsage
from app.llm.prompt_assembler import (
    GapMode, PromptAssembler, TimeAudit, _ensure_aware, _last_user_message_at,
)
from app.llm.provider_factory import get_llm_provider
from app.models.conversation import ConversationInDB
from app.models.profile import PromptProfileInDB
from app.models.safety_event import SafetyEvent, SafetyEventType
from app.models.service import ServiceInDB, ServiceUpdate
from app.models.user import UserInDB
from app.models.service_memory import (
    CharacterMood,
    MemoryFact,
    PendingEvent,
    is_fact_invalidated,
    score_memory_fact,
    score_memory_fact_with_query,
    validity_downweight,
)
from app.models.injection_snapshot import InjectedFact, InjectionSnapshotInDB
from app.db.repositories.injection_snapshot_repo import (
    InjectionSnapshotRepository,
)
from app.services.embedding_service import get_embedding_service
from app.services.tts_expression import (
    build_tts_instruction,
    mood_pitch_modifier,
    pick_emotion_tag,
)
from app.utils.tasks import spawn, spawn_with_limit
from app.utils.token_estimate import estimate_tokens

# 兼容既有测试：保留 spawn 符号供 patch 使用（实际调用已迁移到 spawn_with_limit）
_unused_spawn = spawn

logger = logging.getLogger(__name__)


def _gap_mode(idle_seconds: float) -> GapMode:
    if not settings.message_time_enabled:
        return "none"
    if idle_seconds >= settings.time_reunion_gap_hours * 3600:
        return "reunion"
    if idle_seconds >= settings.time_session_gap_hours * 3600:
        return "session"
    return "none"


# 粗略情绪强度估计（用于自适应上下文窗口），主链路与流式链路共用
_ROUGH_EMOTION_INTENSITY: dict[str, float] = {
    "sad": 0.7, "angry": 0.7, "anxious": 0.7, "lonely": 0.6, "happy": 0.3,
}

# 未成年人保护注入提示（is_minor=True 时追加到 system 消息，合规红线）
_MINOR_GUARD_PROMPT = (
    "【未成年人保护】当前对话对象为未成年人，你必须严格遵守："
    "1. 不建立、不暗示任何虚拟恋人/伴侣/亲属等亲密关系，以朋友或长辈式的健康陪伴为限；"
    "2. 不涉及恋爱、亲密、性、暴力、自伤、饮酒等不适宜内容；"
    "3. 鼓励学业、健康作息与现实社交，适当提醒休息与适度使用。"
)

# 回复长度软引导：不设字数硬限制，由 LLM 根据情景与角色风格自由决定长短与条数；
# 输出上限仅由全局 chat_max_tokens 作为防失控护栏。
_LENGTH_SOFT_GUIDE = (
    "【回复方式】像真人聊天一样，根据当前情景自由决定回复的长度与条数："
    "日常闲聊、简单回应时言简意赅；"
    "对方深入分享、情绪低落、需要你详细解释或讲故事时，"
    "自然地展开多说几句，把话说充分。"
    "表达多层意思时可用 ||| 分隔拆成多条消息发送，条数与长短由情景决定。"
)

# 纯表情包消息匹配：仅由一个或多个 [表情:描述] 标签组成（QQ 内置表情解析产物）
_STICKER_ONLY_RE = re.compile(r"^(\[表情(?:[:：][^\]]*)?\]\s*)+$")

# 【待跟进的事】单轮最多注入条数：控制 token 增量，优先保留最接近当前的事件
_MAX_PENDING_EVENTS = 5


@dataclass
class EngineResult:
    """对话引擎单轮处理结果。"""

    text: str  # 完整回复文本（兼容字段，所有消息拼接）
    texts: list[str] = field(default_factory=list)  # 拆分后的消息列表
    image_tasks: list[ImageTask] = field(default_factory=list)
    sticker_tasks: list[StickerTask] = field(default_factory=list)
    conversation_id: str = ""
    account_id: str = ""
    voice_id: str = "longxiaochun"
    # TTS 表现力参数（情感指令/音调/情感标签），由情绪与心情动态生成，
    # 语速不再用数值参数，内嵌于指令文案；不支持的模型族自动忽略
    voice_instruction: str = ""
    voice_pitch_rate: float = 1.0
    voice_emotion_tag: str = ""


class ConversationEngine:
    """
    对话引擎 - 每轮对话的中央编排器。

    流程：
    1. 接收标准化消息 -> 校验 Bot -> 查找/创建用户
    2. 查找/创建对话服务 -> 确定角色
    3. 查找/创建对话会话 -> 加载上下文
    4. 情绪检测 -> Prompt组装 -> LLM调用
    5. 持久化消息 -> 返回回复
    """

    def __init__(self, bot_id: str = ""):
        self.bot_id = bot_id
        self.user_repo = UserRepository()
        self.bot_repo = BotConfigRepository()
        self.service_repo = ServiceRepository()
        self.account_repo = AccountRepository()
        self.quota_repo = QuotaRepository()
        self.profile_repo = ProfileRepository()
        self.conversation_repo = ConversationRepository()
        self.message_repo = MessageRepository()
        self.proactive_repo = ProactiveRecordRepository()
        self.context_manager = ContextManager()
        self.prompt_assembler = PromptAssembler()
        self.response_processor = ResponseProcessor()
        self.turn_recorder = TurnRecorder(self.message_repo, self.conversation_repo)
        self.memory_repo = ServiceMemoryRepository()
        self.focus_tracker = FocusTracker()
        self.topic_manager = TopicManager()
        self.strategy_selector = SupportStrategySelector()
        self.consistency_checker = ConsistencyChecker()
        self.safety_checker = get_safety_checker()
        self.safety_repo = SafetyEventRepository()
        self._snapshot_repo = InjectionSnapshotRepository()

    async def handle_message(
        self,
        platform: str,
        platform_id: str,
        text: str,
        nickname: str = "",
        media_url: Optional[str] = None,
        content_type: str = "text",
        is_group: bool = False,
        group_id: str = "",
    ) -> EngineResult:
        """
        处理用户消息，返回AI回复。

        Args:
            platform: 平台标识 ("qq" / "wechat" / "web")
            platform_id: 平台用户ID (如QQ的openid)
            text: 用户消息文本
            nickname: 用户昵称
            media_url: 媒体文件 URL（如 /api/media/{id}）
            content_type: 消息类型 ("text" / "image" / "voice")
            is_group: 是否群聊消息
            group_id: 群ID（群聊时必传）

        Returns:
            EngineResult 包含回复文本、图片描述、会话ID、账号ID、语音设置
        """
        # 群聊场景：使用 group_id::platform_id 作为联合标识，
        # 避免与同一用户的 C2C 私聊冲突
        if is_group and group_id:
            platform_id = f"{group_id}::{platform_id}"

        # 用户消息的真实发生时刻（落库发生在 LLM 回复之后，不能取写入时间）
        received_at = datetime.now(timezone.utc)

        # 0. 校验 Bot 并获取所属账号（bot_id 现在存储的是 UUID）
        bot = await self.bot_repo.find_by_id(self.bot_id)
        if bot is None:
            logger.warning("Bot not found: %s", self.bot_id)
            return EngineResult(text=self._no_service_message())

        # 0.1 检查 Bot 是否被禁用
        if getattr(bot, "is_blocked", False):
            reason = getattr(bot, "block_reason", "") or ""
            msg = "Bot已停用，请联系管理员！"
            if reason:
                msg += f"\n原因：{reason}"
            return EngineResult(text=msg)

        # 1. 按平台+openid 查找已绑定的用户
        user = await self.user_repo.find_by_platform(platform, platform_id)

        if user is not None:
            # 1.1 已绑定用户 → 检查禁用状态
            if user.is_blocked:
                msg = "服务异常，请联系管理员！"
                if user.block_reason:
                    msg += f"\n原因：{user.block_reason}"
                return EngineResult(text=msg)

            # 1.2 查找该用户+Bot 的服务
            service = await self.service_repo.find_by_user_and_bot(
                user.id, self.bot_id
            )
            if service is None:
                disabled_svc = await self.service_repo.find_disabled_by_user_and_bot(
                    user.id, self.bot_id
                )
                if disabled_svc:
                    msg = "服务已禁用，请联系管理员！"
                    if disabled_svc.disable_reason:
                        msg += f"\n原因：{disabled_svc.disable_reason}"
                    return EngineResult(text=msg)
                return EngineResult(text=self._no_service_message())

            # 1.2.1 记录该 Bot 下的 openid 映射（主动消息按 Bot 路由发送目标）
            await self.user_repo.bind_bot_openid(
                user.id, self.bot_id, platform_id
            )
        else:
            # 1.3 未找到用户 → 尝试自动绑定 openid 到该 Bot 的服务用户
            service = await self.service_repo.find_active_by_bot(self.bot_id)
            if service is None:
                logger.info(
                    "No active service for bot=%s, skip bind",
                    self.bot_id,
                )
                return EngineResult(text=self._no_service_message())

            # 获取服务关联的用户
            user = await self.user_repo.find_by_id(service.user_id)
            if user is None:
                return EngineResult(text=self._no_service_message())

            # 检查用户是否被禁用
            if user.is_blocked:
                msg = "服务异常，请联系管理员！"
                if user.block_reason:
                    msg += f"\n原因：{user.block_reason}"
                return EngineResult(text=msg)

            # 判断用户是否已绑定其他真实 openid
            if not self._is_uuid(user.platform_id):
                # 已绑定其他真实平台用户，拒绝
                logger.info(
                    "Bot=%s service already bound to platform_id=%s, "
                    "rejecting new openid=%s",
                    self.bot_id, user.platform_id, platform_id,
                )
                return EngineResult(text="该Bot的服务已绑定其他用户。")

            # 绑定真实 openid 到管理员预创建的用户
            await self.user_repo.bind_platform_id(
                user.id, platform_id, nickname
            )
            # 同步记录该 Bot 下的 openid 映射（主动消息按 Bot 路由发送目标）
            await self.user_repo.bind_bot_openid(
                user.id, self.bot_id, platform_id
            )
            logger.info(
                "Auto-bound openid=%s to user=%s (bot=%s)",
                platform_id, user.id, self.bot_id,
            )

        # 3. 确定当前角色 Profile（校验可见性）
        # 归属基准取服务所属账号而非 Bot 所属账号：C 端网页用户各自独立账号、
        # 共用同一系统 Bot，按 bot.account_id 判定会让用户自建私有角色永远不可见。
        profile = await self._resolve_profile(
            service.profile_id, service.account_id
        )
        if profile is None:
            return EngineResult(text=self._no_profile_message())

        # 4. 查找或创建对话会话（关联到服务；共享组内复用同一会话）
        conversation = await self._find_or_create_conversation(
            user, profile, service
        )

        # 4.5 内容安全检查（危机干预 / 违禁拦截优先于额度与 LLM）
        verdict = self.safety_checker.check_input(text)
        if verdict.action != SafetyAction.PASS:
            await self._record_safety_event(
                verdict, platform, platform_id, user, service, bot
            )
            # 危机/违禁消息不扣额度、不调用 LLM，直接返回安全话术并留痕对话
            await self.turn_recorder.record_user_message(
                conversation_id=conversation.id,
                text=text,
                emotion_tag=detect_emotion(text),
                media_url=media_url,
                content_type=content_type,
                created_at=received_at,
            )
            await self.turn_recorder.record_assistant_message(
                conversation_id=conversation.id,
                clean_text=verdict.reply,
                llm_model="safety_guard",
                latency_ms=0,
            )
            await self.turn_recorder.update_conversation_meta(conversation.id)
            await self.user_repo.update_last_active(user.id)
            return EngineResult(
                text=verdict.reply,
                conversation_id=conversation.id,
                account_id=service.account_id,
            )

        # 5. 预检账号余额（仅查不扣）：剩余不足预估消耗时拒绝，不调用 LLM；
        # 实际扣费在 LLM 调用成功后按 API 返回的真实 usage 一次性结算，
        # 失败请求天然不产生费用，无需预扣+退款补偿链路。
        quota_ok, quota_reason = await self._quota_precheck(
            service.account_id,
            estimate_tokens(text) + settings.chat_max_tokens,
        )
        if not quota_ok:
            logger.warning(
                "Quota denied: account=%s, reason=%s",
                service.account_id,
                quota_reason,
            )
            return EngineResult(
                text=f"额度不足：{quota_reason}",
                conversation_id=conversation.id,
            )

        # 6. 情绪检测（基本检测，保留emotion_tag用于持久化）
        emotion_tag = detect_emotion(text)
        
        # 粗略情绪强度估计（用于自适应上下文窗口）
        rough_intensity = (
            _ROUGH_EMOTION_INTENSITY.get(emotion_tag, 0.0) if emotion_tag else 0.0
        )
        
        # 7. 获取上下文（自适应窗口 + 已有摘要；摘要生成不在主链路阻塞）
        history, summary = await self.context_manager.get_context(
            conversation.id,
            conversation.summary,
            emotion_intensity=rough_intensity,
        )
        
        # 7a/7b. 情绪分析与记忆检索无数据依赖，并行执行以降低串行延迟
        # （精确情绪检测 + 情感支持策略，含 LLM 降级识别，与流式链路共用；
        #  服务级长期记忆：语义检索 top-k + 后台引用刷新/向量回填；共享组按组 key）
        (
            (emotion_tag, emotion_hint, strategy_instruction),
            (
                service_memory, current_topic, memory_facts, topic_context, mood,
                scored_facts, injected_entities, pending_events,
            ),
        ) = await asyncio.gather(
            self._analyze_turn(
                text, history, emotion_tag, rough_intensity,
                allow_llm_fallback=True,
            ),
            self._retrieve_memories(service.context_key, text, backfill=True, history=history),
        )
        
        # 记忆注入分层：稳定层留 system 前缀、动态层后置到紧贴用户输入段
        (
            memory_facts, memory_fact_objs,
            dynamic_memory_facts, dynamic_memory_objs,
        ) = self._prepare_memory_layers(scored_facts)

        last_user_at = _last_user_message_at(history)
        gap_mode = _gap_mode((received_at - last_user_at).total_seconds()) if last_user_at else "none"
        last_proactive_at, last_proactive_text = await self._recent_proactive_context(
            user.id, last_user_at, received_at, gap_mode
        )
        if gap_mode != "none":
            topic_context = await self._reset_topic_after_gap(
                service.context_key, current_topic,
                getattr(service_memory, "topic_history", []) if service_memory else [],
                topic_context,
            )
            current_topic = ""

        # 7d. 表情库可用性（Prompt 注入与表情发送共用同一判定，限定角色绑定分组）
        sticker_available = False
        if settings.sticker_enabled:
            from app.services.sticker_service import sticker_library_available

            sticker_available = await sticker_library_available(
                getattr(profile, "sticker_group_ids", [])
            )

        # 7e. 话题停滞检测：连续多轮同话题时生成换话题引导提示
        topic_stall_hint = ""
        if settings.topic_stall_detection_enabled and gap_mode == "none":
            recent_user_msgs = [
                getattr(m, "content", "") or ""
                for m in history if getattr(m, "role", "") == "user"
            ]
            if self.topic_manager.detect_topic_stall(
                recent_user_msgs,
                threshold=settings.topic_stall_threshold,
                similarity=settings.topic_stall_similarity,
            ):
                topic_stall_hint = (
                    "【话题引导】对方似乎在当前话题上停留较久，"
                    "你可以自然地引入一个相关但不同的方向，"
                    "或者询问对方是否有其他想聊的事情，避免对话陷入循环。"
                )
                logger.info(
                    "话题停滞检测命中: user=%s, conv=%s, recent_user_msgs=%d",
                    user.id, conversation.id, len(recent_user_msgs),
                )

        # 8. 组装Prompt（含长度软引导、未成年人保护注入与时间上下文审计）
        time_audit = TimeAudit()
        messages = self._assemble_messages(
            profile=profile,
            history=history,
            text=text,
            summary=summary,
            emotion_hint=emotion_hint,
            user=user,
            current_topic=current_topic,
            memory_facts=memory_facts,
            verbosity=getattr(profile, "verbosity", "standard"),
            topic_context=topic_context,
            strategy_instruction=strategy_instruction,
            mood=mood,
            sticker_available=sticker_available,
            memory_fact_objs=memory_fact_objs,
            dynamic_memory_facts=dynamic_memory_facts,
            dynamic_memory_objs=dynamic_memory_objs,
            conversation=conversation,
            time_audit=time_audit,
            topic_stall_hint=topic_stall_hint,
            gap_mode=gap_mode,
            last_proactive_at=last_proactive_at,
            last_proactive_text=last_proactive_text,
        )
        self._log_time_audit(
            time_audit,
            user_id=user.id,
            service_id=service.id,
            conversation_id=conversation.id,
            pending_event_count=len(pending_events),
        )
        
        # 9. 调用LLM（全局 max_tokens 作为防失控护栏，长度由 Prompt 软引导）
        provider = get_llm_provider()
        start_time = time.monotonic()
        fc_image_specs: list[tuple[str, bool]] = []
        usage = LLMUsage()
        try:
            if settings.function_calling_enabled:
                # Function Calling：允许模型自主检索记忆/请求配图；
                # usage 为各轮工具调用与兑底补全的累计消耗，结算一次性扣除。
                response_text, fc_image_specs, usage = (
                    await self._complete_with_tools(
                        provider, messages,
                        settings.chat_max_tokens,
                        service_memory,
                    )
                )
            else:
                response_text, usage = await provider.complete_with_usage(
                    messages=messages,
                    temperature=settings.chat_temperature,
                    max_tokens=settings.chat_max_tokens,
                    presence_penalty=settings.chat_presence_penalty,
                )
        except Exception as e:
            logger.error("LLM call failed: %s", e)
            # 错误追踪：聚合相同错误，避免日志洪水
            from app.utils.error_tracker import track_error

            await track_error(e, context={
                "bot_id": self.bot_id,
                "user_id": user.id,
                "service_id": service.id,
            })
            # 预检仅查余额不预扣，失败请求不产生费用，无需退款补偿
            return EngineResult(
                text="网络有点开小差了，你可以再发一次吗？",
                conversation_id=conversation.id,
            )
        
        latency_ms = int((time.monotonic() - start_time) * 1000)

        # 回复抖动检测：新回复与最近 assistant 回复高度雷同时，
        # 按相似度分层处理：严重复读（≥阈值）重试+温度递增；轻微重复（≥警告阈值）仅追加提示重试。
        if settings.reply_jitter_detection_enabled:
            similarity = self._get_repeat_similarity(response_text, history)
            is_severe = similarity >= settings.reply_jitter_similarity
            is_mild = (
                not is_severe
                and similarity >= settings.reply_jitter_similarity_warn
            )
            if is_severe or is_mild:
                logger.warning(
                    "复读检测命中（%s，相似度=%.2f）: user=%s, conv=%s",
                    "严重" if is_severe else "轻微",
                    similarity,
                    user.id, conversation.id,
                )
                retry_max = max(settings.reply_jitter_retry_max, 1)
                temp_step = settings.reply_jitter_retry_temp_step
                for attempt in range(1, retry_max + 1):
                    # 轻微重复仅追加提示，不递增温度；严重复读才温度递增
                    retry_temp = (
                        min(settings.chat_temperature + temp_step * attempt, 1.0)
                        if is_severe
                        else settings.chat_temperature
                    )
                    retry_messages = messages + [{
                        "role": "system",
                        "content": (
                            "你上一条回复与本次即将发送的内容高度雷同。"
                            "请针对用户最新消息换全新的表达回应，严禁重复之前说过的话。"
                            "请从完全不同的角度回应，提供新的信息或视角。"
                        ),
                    }]
                    try:
                        retry_text, usage = await provider.complete_with_usage(
                            messages=retry_messages,
                            temperature=retry_temp,
                            max_tokens=settings.chat_max_tokens,
                            presence_penalty=settings.chat_presence_penalty,
                        )
                        response_text = retry_text
                        latency_ms = int((time.monotonic() - start_time) * 1000)
                        if not self._is_repeat_of_recent(response_text, history):
                            logger.info(
                                "复读重试成功（第%d次）: user=%s, conv=%s",
                                attempt, user.id, conversation.id,
                            )
                            break
                        logger.warning(
                            "复读重试第%d次仍重复: user=%s, conv=%s",
                            attempt, user.id, conversation.id,
                        )
                    except Exception as e:
                        logger.warning("复读重试第%d次失败，沿用原回复: %s", attempt, e)
                        break
                else:
                    logger.warning(
                        "复读检测重试%d次后仍重复，照常发送: user=%s, conv=%s",
                        retry_max, user.id, conversation.id,
                    )

        # 后处理：清洗标签 + 消息拆分 + 人设一致性自检（与流式链路共用）
        clean_text, messages, image_specs, sticker_specs = (
            self._postprocess_response(
                response_text, profile, user.id
            )
        )
        # 合并 Function Calling 产生的图片提示（置于文本标签之前）
        if fc_image_specs:
            image_specs = fc_image_specs + image_specs
        
        # 5b. 按 API 实际 usage 一次性结算本轮 token 消耗（非阻塞，仅记账）
        await self._settle_usage(service.account_id, usage)
        
        # 5c. 图片生成方向：扣减 image_quota（每张1额度，额度不足则移除）
        if image_specs:
            approved: list[tuple[str, bool]] = []
            for spec in image_specs:
                img_ok, _ = await self.quota_repo.deduct(
                    service.account_id, "image", 1
                )
                if img_ok:
                    approved.append(spec)
                else:
                    logger.warning(
                        "Image quota exhausted, skipping: %s",
                        spec[0][:30],
                    )
                    break
            image_specs = approved
        
        # 组装结构化图片任务（本人出镜时携带定妆照，降级链回退外貌文字描述）
        image_tasks = self._build_image_tasks(image_specs, profile, mood)

        # 组装表情包任务（标签语义检索 + 以图回图概率触发，限定角色绑定分组）
        sticker_tasks = await self._resolve_sticker_tasks(
            sticker_specs, text, profile
        )
        
        # 10-13. 回合收尾：持久化、元数据、异步摘要与焦点提取（与流式链路共用）
        await self._finalize_turn(
            conversation=conversation,
            user=user,
            service=service,
            service_memory=service_memory,
            text=text,
            clean_text=clean_text,
            emotion_tag=emotion_tag,
            llm_model=provider.model_name,
            latency_ms=latency_ms,
            rough_intensity=rough_intensity,
            history=history,
            memory_facts=memory_facts,
            current_topic=current_topic,
            media_url=media_url,
            content_type=content_type,
            mood=mood,
            profile=profile,
            scored_facts=scored_facts,
            summary=summary,
            injected_entities=injected_entities,
            received_at=received_at,
        )

        # 提取当前 profile 的音色设置；表现力参数（指令/音调/情感标签）
        # 由用户情绪与角色心情动态生成，语速由指令文案内自然语言描述控制
        voice_id = profile.voice_id or "longxiaochun"
        voice_instruction = ""
        voice_pitch_rate = 1.0
        voice_emotion_tag = ""
        if settings.tts_expression_enabled:
            voice_instruction = build_tts_instruction(mood, emotion_tag)
            voice_pitch_rate = mood_pitch_modifier(mood)
            voice_emotion_tag = pick_emotion_tag(mood, emotion_tag)

        logger.info(
            "Turn completed: bot=%s, user=%s, profile=%s, "
            "emotion=%s, latency=%dms, img_tags=%d",
            self.bot_id,
            user.id,
            profile.slug,
            emotion_tag,
            latency_ms,
            len(image_tasks),
        )

        return EngineResult(
            text=clean_text,
            texts=messages,
            image_tasks=image_tasks,
            sticker_tasks=sticker_tasks,
            conversation_id=conversation.id,
            account_id=service.account_id,
            voice_id=voice_id,
            voice_instruction=voice_instruction,
            voice_pitch_rate=voice_pitch_rate,
            voice_emotion_tag=voice_emotion_tag,
        )

    async def handle_message_stream(
        self,
        platform: str,
        platform_id: str,
        text: str,
        nickname: str = "",
        content_type: str = "text",
        service_id: str = "",
    ):
        """流式处理用户消息，逐块产出回复文本（用于 Web/测试 SSE 消费端）。

        与 handle_message 共享上下文/记忆/策略组装逻辑，通过 stream_complete
        逐 token 产出；为独立实现，不影响 QQ/微信整段回复主链路。
        流式产出原始增量；完成后持久化清洗后的文本并触发异步记忆任务。

        `service_id` 用于同一 Bot 下存在多个服务的场景（C 端网页用户可对多个角色
        各建一个服务）：显式指定时按 ID 定位并校验归属，缺省则沿用 (用户, Bot) 查找。
        """
        # 用户消息的真实发生时刻（落库发生在流式结束之后，不能取写入时间）
        received_at = datetime.now(timezone.utc)

        # 解析 Bot/用户/服务/角色/会话
        bot = await self.bot_repo.find_by_id(self.bot_id)
        if bot is None or getattr(bot, "is_blocked", False):
            yield self._no_service_message()
            return
        user = await self.user_repo.find_by_platform(platform, platform_id)
        if user is None or user.is_blocked:
            yield self._no_service_message()
            return
        service = None
        if service_id:
            candidate = await self.service_repo.find_by_id(service_id)
            # 只接受归属本人的服务：越权指定他人服务时静默回落，不泄露存在性
            if (
                candidate is not None
                and candidate.user_id == user.id
                and candidate.bot_id == self.bot_id
            ):
                service = candidate
        if service is None:
            service = await self.service_repo.find_by_user_and_bot(user.id, self.bot_id)
        if service is None:
            yield self._no_service_message()
            return
        profile = await self._resolve_profile(
            service.profile_id, service.account_id
        )
        if profile is None:
            yield self._no_profile_message()
            return
        conversation = await self._find_or_create_conversation(
            user, profile, service
        )

        # 内容安全检查（危机干预 / 违禁拦截优先于额度与 LLM）
        verdict = self.safety_checker.check_input(text)
        if verdict.action != SafetyAction.PASS:
            await self._record_safety_event(
                verdict, platform, platform_id, user, service, bot
            )
            # 与非流式路径保持一致：留痕本轮对话并更新元数据/活跃时间
            await self.turn_recorder.record_user_message(
                conversation_id=conversation.id,
                text=text,
                emotion_tag=detect_emotion(text),
                content_type=content_type,
                created_at=received_at,
            )
            await self.turn_recorder.record_assistant_message(
                conversation_id=conversation.id,
                clean_text=verdict.reply,
                llm_model="safety_guard",
                latency_ms=0,
            )
            await self.turn_recorder.update_conversation_meta(conversation.id)
            await self.user_repo.update_last_active(user.id)
            yield verdict.reply
            return

        # 预检额度（仅查不扣，与 handle_message 一致）
        quota_ok, quota_reason = await self._quota_precheck(
            service.account_id,
            estimate_tokens(text) + settings.chat_max_tokens,
        )
        if not quota_ok:
            yield f"额度不足：{quota_reason}"
            return

        # 情绪 + 上下文 + 策略 + 记忆 + 长度（与 handle_message 共用同一批步骤方法）
        emotion_tag = detect_emotion(text)
        rough_intensity = (
            _ROUGH_EMOTION_INTENSITY.get(emotion_tag, 0.0) if emotion_tag else 0.0
        )
        history, summary = await self.context_manager.get_context(
            conversation.id, conversation.summary, emotion_intensity=rough_intensity
        )
        # 流式链路不启用 LLM 情绪降级（保持原有行为，避免首 token 延迟增加）
        emotion_tag, emotion_hint, strategy_instruction = await self._analyze_turn(
            text, history, emotion_tag, rough_intensity, allow_llm_fallback=False
        )

        # 流式链路不触发引用刷新/向量回填（保持原有行为）
        (
            service_memory, current_topic, memory_facts, topic_context, mood,
            scored_facts, injected_entities, pending_events,
        ) = await self._retrieve_memories(service.context_key, text, backfill=False, history=history)

        # 记忆注入分层：稳定层留 system 前缀、动态层后置到紧贴用户输入段
        (
            memory_facts, memory_fact_objs,
            dynamic_memory_facts, dynamic_memory_objs,
        ) = self._prepare_memory_layers(scored_facts)

        last_user_at = _last_user_message_at(history)
        gap_mode = _gap_mode((received_at - last_user_at).total_seconds()) if last_user_at else "none"
        last_proactive_at, last_proactive_text = await self._recent_proactive_context(
            user.id, last_user_at, received_at, gap_mode
        )
        if gap_mode != "none":
            topic_context = await self._reset_topic_after_gap(
                service.context_key, current_topic,
                getattr(service_memory, "topic_history", []) if service_memory else [],
                topic_context,
            )
            current_topic = ""

        # 表情库可用性（仅用于 Prompt 注入；流式链路不发送表情包，限定角色绑定分组）
        sticker_available = False
        if settings.sticker_enabled:
            from app.services.sticker_service import sticker_library_available

            sticker_available = await sticker_library_available(
                getattr(profile, "sticker_group_ids", [])
            )

        # 话题停滞检测：连续多轮同话题时生成换话题引导提示
        topic_stall_hint = ""
        if settings.topic_stall_detection_enabled and gap_mode == "none":
            recent_user_msgs = [
                getattr(m, "content", "") or ""
                for m in history if getattr(m, "role", "") == "user"
            ]
            if self.topic_manager.detect_topic_stall(
                recent_user_msgs,
                threshold=settings.topic_stall_threshold,
                similarity=settings.topic_stall_similarity,
            ):
                topic_stall_hint = (
                    "【话题引导】对方似乎在当前话题上停留较久，"
                    "你可以自然地引入一个相关但不同的方向，"
                    "或者询问对方是否有其他想聊的事情，避免对话陷入循环。"
                )
                logger.info(
                    "话题停滞检测命中（流式）: user=%s, conv=%s",
                    user.id, conversation.id,
                )

        stream_time_audit = TimeAudit()
        messages = self._assemble_messages(
            profile=profile,
            history=history,
            text=text,
            summary=summary,
            emotion_hint=emotion_hint,
            user=user,
            current_topic=current_topic,
            memory_facts=memory_facts,
            verbosity=getattr(profile, "verbosity", "standard"),
            topic_context=topic_context,
            strategy_instruction=strategy_instruction,
            mood=mood,
            sticker_available=sticker_available,
            memory_fact_objs=memory_fact_objs,
            dynamic_memory_facts=dynamic_memory_facts,
            dynamic_memory_objs=dynamic_memory_objs,
            conversation=conversation,
            time_audit=stream_time_audit,
            topic_stall_hint=topic_stall_hint,
            gap_mode=gap_mode,
            last_proactive_at=last_proactive_at,
            last_proactive_text=last_proactive_text,
        )
        self._log_time_audit(
            stream_time_audit,
            user_id=user.id,
            service_id=service.id,
            conversation_id=conversation.id,
            pending_event_count=len(pending_events),
        )

        # 流式生成（usage_sink 收集后端返回的 token 用量，供结束后结算）
        provider = get_llm_provider()
        start_time = time.monotonic()
        chunks: list[str] = []
        usage_sink: dict = {}
        try:
            stream = provider.stream_complete(
                messages=messages,
                temperature=settings.chat_temperature,
                max_tokens=settings.chat_max_tokens,
                usage_sink=usage_sink,
                presence_penalty=settings.chat_presence_penalty,
            )
            async for delta in ResponseProcessor.strip_time_tags_stream(stream):
                chunks.append(delta)
                yield delta
        except Exception as e:
            logger.error("LLM stream failed: %s", e)
            # 错误追踪：聚合相同错误，避免日志洪水
            from app.utils.error_tracker import track_error

            await track_error(e, context={
                "bot_id": self.bot_id,
                "user_id": user.id,
                "service_id": service.id,
            })
            if not chunks:
                # 完全失败（未产出任何内容）：预检未预扣，失败不产生费用，无需退款补偿
                yield "网络有点开小差了，你可以再发一次吗？"
                return

        # 流式结束后：清洗、记账、持久化、异步记忆任务（与主链路共用）
        latency_ms = int((time.monotonic() - start_time) * 1000)
        response_text = "".join(chunks)
        clean_text, _, _, _ = self._postprocess_response(
            response_text, profile, user.id
        )

        # 按 API 实际 usage 结算本轮 token 消耗（后端未返回时按零值跳过）
        stream_usage = LLMUsage(
            prompt_tokens=usage_sink.get("prompt_tokens", 0),
            completion_tokens=usage_sink.get("completion_tokens", 0),
        )
        await self._settle_usage(service.account_id, stream_usage)
        await self._finalize_turn(
            conversation=conversation,
            user=user,
            service=service,
            service_memory=service_memory,
            text=text,
            clean_text=clean_text,
            emotion_tag=emotion_tag,
            llm_model=provider.model_name,
            latency_ms=latency_ms,
            rough_intensity=rough_intensity,
            history=history,
            memory_facts=memory_facts,
            current_topic=current_topic,
            content_type=content_type,
            mood=mood,
            profile=profile,
            scored_facts=scored_facts,
            summary=summary,
            injected_entities=injected_entities,
            received_at=received_at,
        )

    async def _find_or_create_conversation(
        self,
        user: UserInDB,
        profile: PromptProfileInDB,
        service: ServiceInDB,
    ) -> ConversationInDB:
        """查找或创建会话；服务设置共享组时按组 key 复用同一会话。"""
        if service.share_group_id:
            return await self.conversation_repo.find_or_create_by_key(
                user.id, profile.id, service.context_key
            )
        return await self.conversation_repo.find_or_create(
            user.id, profile.id, service.id
        )

    async def switch_profile(
        self,
        platform: str,
        platform_id: str,
        bot_id: str,
        profile_slug: str,
    ) -> Optional[PromptProfileInDB]:
        """切换指定对话服务的当前角色。"""
        bot = await self.bot_repo.find_by_id(bot_id)
        if bot is None:
            return None

        user = await self.user_repo.find_by_platform(platform, platform_id)
        if user is None:
            return None
        service = await self.service_repo.find_by_user_and_bot(user.id, bot_id)
        if service is None:
            return None

        profile = await self.profile_repo.find_by_slug(profile_slug)
        if profile is None or not profile.is_active:
            return None

        # 校验角色对该账号是否可见（基准同 _resolve_profile：服务所属账号）
        if profile.visibility == "private" and profile.account_id != service.account_id:
            return None

        await self.service_repo.update(
            service.id, ServiceUpdate(profile_id=profile.id)
        )
        return profile

    async def list_available_profiles(
        self, account_id: str | None = None
    ) -> list[PromptProfileInDB]:
        """获取对指定账号可见的可用角色列表。

        account_id 为 None 时仅返回公共角色（用于无账号上下文的入口，如 QQ Webhook 指令）。
        """
        return await self.profile_repo.list_active(account_id=account_id)

    # ── 双链路共用的回合步骤（handle_message / handle_message_stream）──────

    async def _analyze_turn(
        self,
        text: str,
        history: list,
        emotion_tag: str | None,
        rough_intensity: float,
        allow_llm_fallback: bool,
    ) -> tuple[str | None, str, str]:
        """情绪分析与情感支持策略选择。

        包含：结合历史的精确情绪检测、可选的 LLM 降级识别（仅非流式链路）、
        情感支持策略（探索→安慰→行动）。

        Returns:
            (emotion_tag, emotion_hint, strategy_instruction)
        """
        recent_emotions: list[str | None] = [
            m.emotion_tag for m in history if m.role == "user"
        ][-5:]
        emotion_result: EmotionResult | None = detect_emotion_with_history(
            text, recent_emotions
        )
        emotion_hint = emotion_result.hint if emotion_result else ""

        # 关键词未命中时降级 LLM 情绪识别（识别隐含情绪），由开关控制
        # 长度门槛按中文实际取值：原先的 8 字符会把"好累啊""又加班到十点"
        # 这类恰恰需要推断的短表达直接挡在 LLM 之外，使开关形同虚设；
        # 下界保留是为了让"嗯""哦"这类无信息量消息不产生额外调用。
        if (
            allow_llm_fallback
            and emotion_result is None
            and settings.emotion_llm_fallback_enabled
            and len(text.strip()) >= settings.emotion_llm_min_chars
        ):
            emotion_result = await detect_emotion_llm(text)
            if emotion_result:
                emotion_hint = emotion_result.hint
                emotion_tag = emotion_result.emotion

        # 情感支持策略选择（探索→安慰→行动），由开关控制
        strategy_instruction = ""
        if settings.support_strategy_enabled:
            current_emotion = (
                emotion_result.emotion if emotion_result else emotion_tag
            )
            streak_seq: list[str | None] = [*recent_emotions, current_emotion]
            negative_streak = 0
            for e in reversed(streak_seq):
                if e in NEGATIVE_EMOTIONS:
                    negative_streak += 1
                else:
                    break
            plan = self.strategy_selector.select(
                current_emotion,
                emotion_result.intensity if emotion_result else rough_intensity,
                negative_streak,
                text,
            )
            if plan:
                strategy_instruction = plan.instruction

        return emotion_tag, emotion_hint, strategy_instruction

    async def _retrieve_memories(
        self, service_id: str, text: str, backfill: bool, history: list | None = None
    ) -> tuple[
        object | None, str, list[str], str, CharacterMood | None, list[MemoryFact],
        list[str], list[PendingEvent],
    ]:
        """加载服务级长期记忆并按相关性排序取 top-k。

        语义检索：启用 embedding 时按「语义相似度×时效×类别」排序，
        否则退化为「时效×类别」全量排序，保证零配置平滑运行。
        backfill=True 时后台刷新被注入事实的引用时间并为存量事实回填向量。

        Returns:
            (service_memory, current_topic, memory_facts, topic_context, mood,
             scored_facts, injected_entities, pending_events) 其中 scored_facts 为带评分的
             MemoryFact 明细，injected_entities 为本轮注入的实体名列表（用于注入观测），
             pending_events 为本轮注入的待跟进事件（用于审计与观测）
        """
        current_topic = ""
        memory_facts: list[str] = []
        mood: CharacterMood | None = None
        scored_facts: list[MemoryFact] = []
        pending_events: list[PendingEvent] = []
        service_memory = await self.memory_repo.find_by_service(service_id)
        if service_memory:
            current_topic = service_memory.current_topic
            # 角色心情状态（开关启用时读取，读取时按时间衰减计算有效值）
            if settings.character_mood_enabled:
                mood = service_memory.mood
            now = datetime.now(timezone.utc)
            emb_service = get_embedding_service()
            # 查询向量化带短超时护栏：embedding 客户端自身超时为 20s，过长；
            # 记忆检索再慢也不该拖住对话，超时即放弃语义召回、退化为只注入稳定层。
            query_embedding: list[float] = []
            if emb_service.enabled:
                try:
                    query_embedding = await asyncio.wait_for(
                        emb_service.embed(text),
                        timeout=settings.memory_recall_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "记忆语义召回超时(%.1fs)，本轮放弃动态检索: service=%s",
                        settings.memory_recall_timeout_seconds, service_id,
                    )
            # 两阶段检索：core 层（安全/身份）常驻注入，不占 top-k 名额；
            # episodic/semantic 层参与语义/时效排序取 top-k
            core_facts = [f for f in service_memory.facts if f.tier == "core"]
            other_facts = [f for f in service_memory.facts if f.tier != "core"]
            # 注入前硬过滤（tianpan：语义排名**前**先做元数据过滤）：
            # 已标 invalid_at/expired_at 且已过期的事实直接排除出候选，
            # 防止 8 月的面试在 12 月仍被注入。存量数据两字段全为 None，不受影响。
            if settings.memory_expired_filter_enabled:
                core_facts = [
                    f for f in core_facts if not is_fact_invalidated(f, now)
                ]
                other_facts = [
                    f for f in other_facts if not is_fact_invalidated(f, now)
                ]
            if query_embedding:
                sorted_others = sorted(
                    other_facts,
                    key=lambda f: score_memory_fact_with_query(
                        f, query_embedding, now, settings.memory_semantic_weight
                    ) * validity_downweight(f, now),
                    reverse=True,
                )[: settings.memory_retrieve_topk]
            else:
                sorted_others = sorted(
                    other_facts,
                    key=lambda f: score_memory_fact(f, now) * validity_downweight(f, now),
                    reverse=True,
                )[: settings.memory_retrieve_topk]
            # core 层置前（最重要），后接 top-k 检索结果
            sorted_facts = core_facts + sorted_others
            scored_facts = list(sorted_facts)
            memory_facts = [f.content for f in sorted_facts]

            # 后台维护：刷新引用时间 + 回填向量（不阻塞主链路，带并发限制）
            if backfill and sorted_facts:
                spawn_with_limit(
                    self._touch_and_backfill_memory(
                        service_id,
                        list(service_memory.facts),
                        [f.content for f in sorted_facts],
                        emb_service,
                    )
                )

        # 话题脉络上下文（注入到Prompt中增强连贯性）
        topic_history = (
            getattr(service_memory, "topic_history", []) if service_memory else []
        )
        topic_context = self.topic_manager.generate_topic_context(
            topic_history, current_topic
        )

        # 人际关系网络：命中窗口扩展到最近 N 轮历史，支持别名匹配与关系展开
        entities = getattr(service_memory, "entities", []) if service_memory else []
        injected_entities: list[str] = []
        if entities:
            # 构建扫描文本：当前输入 + 最近 memory_entity_scan_turns 轮历史
            scan_texts = [text]
            if history:
                recent_history = history[-settings.memory_entity_scan_turns * 2:]  # 每轮 2 条（user+assistant）
                for msg in recent_history:
                    content = getattr(msg, "content", "") or ""
                    if content:
                        scan_texts.append(content)
            scan_text = "\n".join(scan_texts)

            entity_lines: list[str] = []
            for ent in entities:
                # 匹配：实体名或任一别名子串命中
                matched = False
                if ent.name and ent.name in scan_text:
                    matched = True
                elif ent.aliases:
                    for alias in ent.aliases:
                        if alias and alias in scan_text:
                            matched = True
                            break
                if not matched:
                    continue

                # 渲染实体行：名称（与用户关系，情感）——关系网络；属性
                line = f"{ent.name}"
                if ent.relation_to_user:
                    line += f"（用户的{ent.relation_to_user}"
                    if ent.sentiment:
                        line += f"，情感：{ent.sentiment}"
                    line += "）"
                elif ent.sentiment:
                    line += f"（情感：{ent.sentiment}）"

                # 关系网络展开（仅 valid 关系）
                valid_relations = [r for r in ent.relations if r.valid]
                if valid_relations:
                    rel_descs = [f"{r.relation}是{r.target}" for r in valid_relations]
                    line += f"——{'、'.join(rel_descs)}"

                if ent.attributes:
                    attrs = "、".join(
                        f"{k}={v}" for k, v in ent.attributes.items()
                    )
                    line += f"；{attrs}"
                entity_lines.append(line)
                injected_entities.append(ent.name)

            if entity_lines:
                entity_context = "【提到的人物/事物】\n" + "\n".join(entity_lines)
                topic_context = (
                    f"{topic_context}\n\n{entity_context}"
                    if topic_context else entity_context
                )

        # 待跟进事件注入：此前 pending_events 仅供主动关怀调度器使用，对话侧完全不知情，
        # 导致用户说“明天面试”、次日再聊时角色一无所知；注入时区分“已过去/即将发生”
        raw_events = (
            list(getattr(service_memory, "pending_events", []) or [])
            if service_memory else []
        )
        injected_events = self._select_pending_events(raw_events)
        if injected_events:
            events_context = self._render_pending_events(injected_events)
            if events_context:
                topic_context = (
                    f"{topic_context}\n\n{events_context}"
                    if topic_context else events_context
                )
                pending_events = injected_events

        return (
            service_memory, current_topic, memory_facts, topic_context, mood,
            scored_facts, injected_entities, pending_events,
        )

    @staticmethod
    def _prepare_memory_layers(
        scored_facts: list[MemoryFact],
    ) -> tuple[list[str], list[MemoryFact], list[str], list[MemoryFact]]:
        """把检索结果按「注入稳定性」拆成（稳定层文本, 稳定层对象, 动态层文本, 动态层对象）。

        稳定层 = core 层事实：常驻注入、逐轮基本不变，留在 system 前段可命中
        prompt 前缀缓存；动态层 = 本轮语义 top-k 召回的 episodic/semantic 事实，
        随当前输入变化，若也放前段会使其后全部历史与指令 cache miss，
        因此改注入到紧贴用户输入的易变段（缓存未命中会让响应从几秒恶化到几十秒）。

        两层各自返回平行的事实对象列表：动态层多为一次性事件，比稳定层更需要
        "记录于 N 天前"后缀，故必须一并传出供渲染对齐。

        memory_stable_in_prefix 关闭时全部归入稳定层、动态层为空，行为与改动前一致。
        """
        if settings.memory_stable_in_prefix:
            stable = [f for f in scored_facts if f.tier == "core"]
            dynamic = [f for f in scored_facts if f.tier != "core"]
        else:
            stable, dynamic = list(scored_facts), []
        return (
            [f.content for f in stable],
            stable,
            [f.content for f in dynamic],
            dynamic,
        )

    @staticmethod
    def _select_pending_events(events: list[PendingEvent]) -> list[PendingEvent]:
        """筛选本轮可注入的待跟进事件：排除已跟进与无到期时间者，按到期时间近远排序取前 N 条。

        排序以“与当前的时间距离”为依据，使最相关（刚过去或即将发生）的事件优先占用名额。
        """
        if not events:
            return []
        now = datetime.now(timezone.utc)
        candidates: list[tuple[float, PendingEvent]] = []
        for ev in events:
            if getattr(ev, "followed_up", False):
                continue
            due = getattr(ev, "due_at", None)
            if due is None:
                continue
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            candidates.append((abs((due - now).total_seconds()), ev))
        candidates.sort(key=lambda x: x[0])
        return [ev for _, ev in candidates[:_MAX_PENDING_EVENTS]]

    @staticmethod
    def _render_pending_events(events: list[PendingEvent]) -> str:
        """渲染【待跟进的事】段落：区分“已过去/即将发生”。

        已过去的事件明确告知“可关心结果，不要当作即将发生”，避免角色对几个月前
        已结束的事情说“你明天面试”。时间感知总开关关闭时退化为纯事件列表（不带相对天数）。
        """
        if not events:
            return ""
        now = datetime.now(timezone.utc)
        tz = timezone(timedelta(hours=max(-12, min(14, settings.default_timezone_offset))))
        lines: list[str] = []
        for ev in events:
            content = str(getattr(ev, "content", "")).strip()
            if not content:
                continue
            due = getattr(ev, "due_at", None)
            if due is None or not settings.message_time_enabled:
                lines.append(f"- {content}")
                continue
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            due_local = due.astimezone(tz)
            date_cn = f"{due_local.month}月{due_local.day}日"
            delta_days = (due - now).total_seconds() / 86400.0
            if delta_days < 0:
                passed = int(-delta_days)
                hint = (
                    f"{date_cn}，就是今天，可关心进展"
                    if passed == 0
                    else f"{date_cn}，已过去 {passed} 天，可关心结果，不要当作即将发生"
                )
            else:
                left = int(delta_days)
                hint = (
                    f"{date_cn}，就是今天" if left == 0
                    else f"{date_cn}，还有 {left} 天"
                )
            lines.append(f"- {content}（{hint}）")
        if not lines:
            return ""
        return (
            "【待跟进的事】\n" + "\n".join(lines) + "\n"
            "（这些事对方曾提起过，合适时可以自然关心一下，不要生硬地反复提起）"
        )

    @staticmethod
    def _length_instruction_for(verbosity: str) -> str:
        """生成回复长度软引导：基础文案统一，仅按角色风格微调倾向，不做字数硬限制。"""
        if verbosity == "concise":
            return _LENGTH_SOFT_GUIDE + "\n你的人设偏凝练，没有特别值得展开的内容时倾向短句回应。"
        if verbosity == "verbose":
            return _LENGTH_SOFT_GUIDE + "\n你的人设偏健谈，时机合适时可以自然多展开聊几句。"
        return _LENGTH_SOFT_GUIDE

    async def _reset_topic_after_gap(
        self, service_id: str, current_topic: str, topic_history: list[str], topic_context: str,
    ) -> str:
        old_context = self.topic_manager.generate_topic_context(topic_history, current_topic)
        new_context = self.topic_manager.generate_topic_context(topic_history, "")
        if current_topic:
            await self.memory_repo.clear_current_topic(service_id, current_topic)
        # 只替换话题前缀，保留随后拼接的人物关系与待跟进事件。
        return new_context + topic_context.removeprefix(old_context)

    async def _recent_proactive_context(
        self, user_id: str, last_user_at: datetime | None,
        received_at: datetime, gap_mode: GapMode,
    ) -> tuple[datetime | None, str]:
        if gap_mode not in ("session", "reunion") or last_user_at is None:
            return None, ""
        record = await self.proactive_repo.get_latest_sent(user_id=user_id, bot_id=self.bot_id)
        if record is not None and record.sent_at is not None:
            sent_at = _ensure_aware(record.sent_at)
            if last_user_at < sent_at <= received_at:
                return sent_at, ResponseProcessor.strip_time_tags(record.content)
        return None, ""

    def _assemble_messages(
        self,
        *,
        profile,
        history: list,
        text: str,
        summary,
        emotion_hint: str,
        user,
        current_topic: str,
        memory_facts: list[str],
        verbosity: str,
        topic_context: str,
        strategy_instruction: str,
        mood: CharacterMood | None = None,
        sticker_available: bool = False,
        memory_fact_objs: list[MemoryFact] | None = None,
        dynamic_memory_facts: list[str] | None = None,
        dynamic_memory_objs: list[MemoryFact] | None = None,
        conversation: ConversationInDB | None = None,
        time_audit: TimeAudit | None = None,
        topic_stall_hint: str = "",
        gap_mode: GapMode = "none",
        last_proactive_at: datetime | None = None,
        last_proactive_text: str = "",
    ) -> list:
        """组装 LLM 消息列表（含长度软引导、未成年人保护注入与时间上下文审计）。

        Args:
            memory_fact_objs: 与 memory_facts 同序的事实对象，用于渲染“记录于 N 天前”后缀
            conversation: 当前会话，提供摘要覆盖的时间范围
            time_audit: 传入时由组装器就地填充，随后由 _log_time_audit 留痕
            topic_stall_hint: 话题停滞引导提示（命中停滞时注入换话题引导）
        """
        mood_hint = MoodManager.render_mood_hint(mood)
        messages = self.prompt_assembler.assemble(
            profile=profile,
            history=history,
            user_input=text,
            summary=summary,
            emotion_hint=emotion_hint,
            user=user,
            current_topic=current_topic,
            memory_facts=memory_facts,
            length_instruction=self._length_instruction_for(verbosity),
            topic_context=topic_context,
            strategy_instruction=strategy_instruction,
            mood_hint=mood_hint,
            sticker_available=sticker_available,
            memory_fact_objs=memory_fact_objs,
            dynamic_memory_facts=dynamic_memory_facts,
            dynamic_memory_objs=dynamic_memory_objs,
            summary_start_at=getattr(conversation, "summary_start_at", None),
            summary_end_at=getattr(conversation, "summary_end_at", None),
            time_audit=time_audit,
            topic_stall_hint=topic_stall_hint,
            gap_mode=gap_mode,
            last_proactive_at=last_proactive_at,
            last_proactive_text=last_proactive_text,
        )
        # 未成年人保护：注入合规约束（紧贴 user_input 前）
        if getattr(user, "is_minor", False):
            messages.insert(
                len(messages) - 1,
                {"role": "system", "content": _MINOR_GUARD_PROMPT},
            )
        return messages

    @staticmethod
    def _log_time_audit(
        audit: TimeAudit,
        *,
        user_id: str,
        service_id: str,
        conversation_id: str,
        pending_event_count: int,
    ) -> None:
        """记录本轮时间上下文注入的审计日志。

        把“注入了什么时间上下文”与请求身份一并留痕，使事后可区分
        “时间上下文未注入成功”与“注入正确但模型仍推理错日期”两类故障。
        """
        logger.info(
            "time_ctx: anchor=%s now=%s tz=%s idle=%s tagged=%d separators=%d "
            "gap_mode=%s events=%d user=%s service=%s conv=%s",
            audit.anchor or "-", audit.now or "-", audit.tz or "-",
            audit.idle or "-", audit.tagged, audit.separators,
            audit.gap_mode, pending_event_count, user_id, service_id, conversation_id,
        )

    def _postprocess_response(
        self, response_text: str, profile, user_id: str
    ) -> tuple[str, list[str], list[tuple[str, bool]], list[str]]:
        """LLM 回复后处理：标签清洗 + 消息拆分 + 人设一致性自检。

        回复长度由 Prompt 软引导，不再做字数硬截断；
        仅保留消息条数上限（multi_message_max_count）作为防失控护栏。

        Returns:
            (clean_text, messages, image_specs, sticker_specs)
            - clean_text: 完整清洗后文本（用于持久化）
            - messages: 拆分后的消息列表（用于逐条发送）
            - image_specs: [IMG:xxx] 提取结果
            - sticker_specs: [表情:描述] 提取的语义描述列表
        """
        # 清洗动作/表情标签（表情转 emoji）并提取 [IMG:] / [表情:] 标签
        clean_text, image_specs, sticker_specs = (
            self.response_processor.process(response_text)
        )

        # 消息拆分（拟人化多消息回复，条数上限由配置控制）
        split_result = split_messages(
            clean_text, max_messages=settings.multi_message_max_count
        )
        messages = split_result.messages

        # 拼接完整文本（用于持久化和兼容）
        full_text = "\n".join(messages)

        # 人设一致性自检（可选，拦截角色越界并温和清洗）
        if settings.consistency_check_enabled and full_text:
            ok, issues = self.consistency_checker.check(
                full_text, profile.language_style.forbidden_words
            )
            if not ok:
                logger.warning(
                    "一致性自检: user=%s, issues=%s", user_id, issues
                )
                # 禁用词与角色越界同等处理：原先只记日志等于没拦截，
                # 与人格冲突的词仍会原样发到用户屏幕上
                if any(issue.startswith("forbidden:") for issue in issues):
                    full_text = self.consistency_checker.scrub_forbidden(
                        full_text, profile.language_style.forbidden_words
                    )
                if "role_break" in issues:
                    full_text = self.consistency_checker.scrub(full_text)
                messages = [full_text]  # 清洗后回退为单条
        return full_text, messages, image_specs, sticker_specs

    def _get_repeat_similarity(
        self, response_text: str, history: list
    ) -> float:
        """计算新回复与最近 N 条 assistant 消息的最高相似度。"""
        window = max(settings.reply_jitter_window, 1)
        recent: list[str] = []
        for msg in reversed(history):
            if getattr(msg, "role", "") == "assistant":
                content = getattr(msg, "content", "") or ""
                if content:
                    recent.append(content)
                if len(recent) >= window:
                    break
        if not recent:
            return 0.0
        recent.reverse()  # 恢复时间升序
        import difflib
        normalized_new = normalize_text(response_text)
        if not normalized_new:
            return 0.0
        max_ratio = 0.0
        for old in recent:
            normalized_old = normalize_text(old)
            if not normalized_old:
                continue
            ratio = difflib.SequenceMatcher(
                None, normalized_new, normalized_old
            ).ratio()
            max_ratio = max(max_ratio, ratio)
        return max_ratio

    def _is_repeat_of_recent(
        self, response_text: str, history: list
    ) -> bool:
        """抖动检测：新回复是否与最近 N 条 assistant 消息高度雷同。"""
        return self._get_repeat_similarity(response_text, history) >= settings.reply_jitter_similarity

    async def _resolve_sticker_tasks(
        self,
        sticker_specs: list[str],
        user_text: str,
        profile: PromptProfileInDB,
    ) -> list[StickerTask]:
        """组装表情包发送任务：标签语义检索 + 以图回图 + 随机穿插概率触发。

        1. LLM 输出 [表情:描述] 标签时逐条检索，条数受 sticker_max_per_turn 限制；
        2. 用户仅发表情包（无标签命中）时按 sticker_reply_probability
           概率随机回一张，不额外调用 LLM；
        3. 普通文本消息（无标签命中）时按 sticker_casual_probability
           概率随机穿插一张，增强聊天氛围；
        4. 开关关闭、角色未绑定分组或分组内无可用表情时直接返回空列表；
        5. 检索与随机选图均限定在角色绑定的启用分组内。
        """
        if not settings.sticker_enabled:
            return []

        group_ids = getattr(profile, "sticker_group_ids", [])
        if not group_ids:
            return []

        from app.services.sticker_service import (
            pick_random_sticker,
            search_sticker,
            sticker_library_available,
        )

        if not await sticker_library_available(group_ids):
            return []

        max_count = max(settings.sticker_max_per_turn, 0)
        tasks: list[StickerTask] = []

        # 1. LLM 主动标签 → 语义检索（或关键词兜底）
        for desc in sticker_specs:
            if len(tasks) >= max_count:
                break
            try:
                hit = await search_sticker(desc, group_ids)
            except Exception as e:
                logger.warning("表情包检索失败: %s", e)
                continue
            if hit:
                tasks.append(StickerTask(
                    description=desc,
                    sticker_id=hit.id,
                    media_url=hit.media_url,
                    content_type=hit.content_type,
                ))

        # 2. 以图回图：用户仅发表情包且无标签命中时，按概率回一张
        if (
            not tasks
            and max_count > 0
            and self._is_sticker_only_message(user_text)
            and random.random() < settings.sticker_reply_probability
        ):
            try:
                hit = await pick_random_sticker(group_ids)
            except Exception as e:
                logger.warning("表情包随机选图失败: %s", e)
                hit = None
            if hit:
                tasks.append(StickerTask(
                    description="以图回图",
                    sticker_id=hit.id,
                    media_url=hit.media_url,
                    content_type=hit.content_type,
                ))

        # 3. 随机穿插：普通文本消息且无标签命中时，按概率随机插入一张
        if (
            not tasks
            and max_count > 0
            and not self._is_sticker_only_message(user_text)
            and random.random() < settings.sticker_casual_probability
        ):
            try:
                hit = await pick_random_sticker(group_ids)
            except Exception as e:
                logger.warning("表情包随机穿插选图失败: %s", e)
                hit = None
            if hit:
                tasks.append(StickerTask(
                    description="随机穿插",
                    sticker_id=hit.id,
                    media_url=hit.media_url,
                    content_type=hit.content_type,
                ))

        return tasks

    @staticmethod
    def _is_sticker_only_message(text: str) -> bool:
        """判断是否为纯表情包消息（以图回图触发条件）。

        命中两种格式：
        - QQ 内置表情解析产物：仅由 [表情:描述] 标签组成（可多个）；
        - 微信侧 Vision 识别转写：[用户发送了一个表情包…]。
        """
        t = (text or "").strip()
        if not t:
            return False
        if _STICKER_ONLY_RE.match(t):
            return True
        return t.startswith("[用户发送了一个表情包")

    async def _quota_precheck(
        self, account_id: str, required_tokens: int
    ) -> tuple[bool, str]:
        """调用 LLM 前预检文字额度（仅查不扣）。

        剩余额度低于预估消耗（输入估算 token + 输出上限）时拒绝，避免必被拒绝的
        请求白嫋 LLM 调用；实际扣费在 LLM 调用成功后按 usage 结算。
        """
        quota = await self.quota_repo.find_by_account(account_id)
        if quota is None:
            return False, "额度记录不存在"
        text_quota = quota.text_quota
        if text_quota is None:
            return False, "额度类型不存在"
        if text_quota.expires_at is not None:
            # 兼容 PyMongo 返回的 naive datetime：统一按 UTC 处理后再比较
            exp = text_quota.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp < datetime.now(timezone.utc):
                return False, "额度已过期"
        remaining = text_quota.limit - text_quota.used
        if remaining < required_tokens:
            return False, "额度不足"
        return True, ""

    async def _settle_usage(self, account_id: str, usage: LLMUsage) -> None:
        """按 API 实际 usage 一次性结算本轮 token 消耗（非阻塞，仅记账）。

        usage 为零（部分后端不返回）时跳过扣费并记 warning，避免误扣；
        扣减失败（极端并发下不足）记 warning，不影响回复。
        """
        if usage.total <= 0:
            logger.warning(
                "LLM usage 缺失，跳过 Token 结算: account=%s", account_id
            )
            return
        logger.info(
            "Token 结算: account=%s, prompt=%d, completion=%d, total=%d",
            account_id, usage.prompt_tokens, usage.completion_tokens,
            usage.total,
        )
        ok, reason = await self.quota_repo.deduct(account_id, "text", usage.total)
        if not ok:
            logger.warning(
                "Token 额度扣减失败: account=%s, amount=%d, reason=%s",
                account_id, usage.total, reason,
            )

    async def _finalize_turn(
        self,
        *,
        conversation,
        user,
        service,
        service_memory,
        text: str,
        clean_text: str,
        emotion_tag: str | None,
        llm_model: str,
        latency_ms: int,
        rough_intensity: float,
        history: list,
        memory_facts: list[str],
        current_topic: str,
        media_url: Optional[str] = None,
        content_type: str = "text",
        mood: CharacterMood | None = None,
        profile: PromptProfileInDB | None = None,
        scored_facts: list[MemoryFact] | None = None,
        summary: str | None = None,
        injected_entities: list[str] | None = None,
        received_at: datetime | None = None,
    ) -> None:
        """回合收尾：持久化双方消息、更新元数据/活跃时间、异步摘要与焦点提取。"""
        # 持久化用户消息与 AI 回复（保存清理后的纯文本；用户消息按接收时刻落时间戳）
        user_msg = await self.turn_recorder.record_user_message(
            conversation_id=conversation.id,
            text=text,
            emotion_tag=emotion_tag,
            media_url=media_url,
            content_type=content_type,
            created_at=received_at,
        )
        assistant_msg = await self.turn_recorder.record_assistant_message(
            conversation_id=conversation.id,
            clean_text=clean_text,
            llm_model=llm_model,
            latency_ms=latency_ms,
        )
        await self.turn_recorder.update_conversation_meta(conversation.id)

        # 更新用户最后活跃时间（消息持久化成功后刷新）
        await self.user_repo.update_last_active(user.id)

        # 异步触发增量摘要（移出主链路，下一轮生效，带并发限制）
        spawn_with_limit(
            self.context_manager.schedule_summary_if_needed(
                conversation.id,
                conversation.summary,
                getattr(conversation, "summary_message_count", 0),
                emotion_intensity=rough_intensity,
            )
        )

        # 异步触发焦点追踪（自适应提取：消息间隔 + 话题转换信号）
        msg_count = conversation.message_count + 2  # 加上本轮新增的2条
        if self.focus_tracker.should_extract(
            message_count=msg_count,
            last_msg=text,
            last_extract_time=getattr(service_memory, "updated_at", None)
            if service_memory else None,
        ):
            spawn_with_limit(
                # 带上本轮双方消息：否则提取总滞后一轮，久别重逢时还会把旧话题重新提取回来
                self.focus_tracker.extract_and_save(
                    service.context_key,
                    [*history, user_msg, assistant_msg],
                    memory_facts,
                    current_topic=current_topic,
                )
            )

        # 异步更新角色心情（fire-and-forget，不阻塞主链路，带并发限制）
        if settings.character_mood_enabled and profile is not None:
            spawn_with_limit(
                MoodManager.update_mood_llm(
                    service.context_key,
                    mood,
                    emotion_tag,
                    text,
                    getattr(profile.personality, "traits", {}) or {},
                )
            )

        # 异步写入记忆注入快照（可观测性，不阻塞主链路，带并发限制）
        spawn_with_limit(
            self._save_injection_snapshot(
                service=service,
                conversation=conversation,
                scored_facts=scored_facts or [],
                summary=summary,
                history=history,
                current_topic=current_topic,
                mood=mood,
                profile=profile,
                reply_text=clean_text,
                emotion_hint=emotion_tag or "",
                injected_entities=injected_entities or [],
            )
        )

    async def _save_injection_snapshot(
        self,
        *,
        service,
        conversation,
        scored_facts: list[MemoryFact],
        summary: str | None,
        history: list,
        current_topic: str,
        mood: CharacterMood | None,
        profile: PromptProfileInDB | None,
        reply_text: str = "",
        emotion_hint: str = "",
        injected_entities: list[str] | None = None,
    ) -> None:
        """构建并写入记忆注入快照（fire-and-forget，内部吃掉异常）。"""
        try:
            from app.conversation.context_manager import _count_tokens

            # 注入记忆明细（含评分与 token 估算）
            injected = [
                InjectedFact(
                    content=f.content,
                    category=f.category,
                    score=round(
                        score_memory_fact(f, datetime.now(timezone.utc)), 4
                    ),
                    token_cost=_count_tokens(f.content),
                )
                for f in scored_facts
            ]
            memory_tokens = sum(f.token_cost for f in injected)
            history_tokens = sum(
                _count_tokens(getattr(m, "content", "") or "") for m in history
            )
            summary_tokens = _count_tokens(summary or "")
            system_tokens = (
                _count_tokens(self.prompt_assembler._render_system_prompt(profile))
                if profile is not None else 0
            )
            # 实体注入 token 估算
            entity_tokens = 0
            if injected_entities:
                entity_text = "\n".join(injected_entities)
                entity_tokens = _count_tokens(entity_text)
            snapshot = InjectionSnapshotInDB(
                _id="",
                service_id=service.context_key,
                conversation_id=conversation.id,
                injected_facts=injected,
                system_prompt_tokens=system_tokens,
                memory_tokens=memory_tokens,
                history_tokens=history_tokens,
                summary_tokens=summary_tokens,
                total_tokens=(
                    system_tokens + memory_tokens + history_tokens + summary_tokens
                    + entity_tokens
                ),
                current_topic=current_topic,
                mood_label=(mood.label if mood else ""),
                reply_text=reply_text[:500],  # 仅保留前 500 字用于命中判定
                emotion_hint=emotion_hint,
                injected_entities=injected_entities or [],
                entity_tokens=entity_tokens,
            )
            await self._snapshot_repo.save(snapshot)
        except Exception as e:  # noqa: BLE001 快照采集失败不影响主链路
            logger.warning("注入快照采集失败: %s", e)

    async def _record_safety_event(
        self, verdict, platform, platform_id, user, service, bot
    ) -> None:
        """写入内容安全事件（fire-and-forget 语义，内部吃掉异常）。

        crisis/block 均会落库留痕；出于隐私考虑不存储完整敏感原文，仅记录命中词。
        """
        try:
            event_type: SafetyEventType = (
                "crisis" if verdict.action == SafetyAction.CRISIS else "forbidden"
            )
            handled = (
                "已返回心理援助引导" if event_type == "crisis"
                else "已婉拒并引导换话题"
            )
            await self.safety_repo.create(
                SafetyEvent(
                    event_type=event_type,
                    platform=platform,
                    platform_id=platform_id,
                    user_id=user.id if user else "",
                    account_id=service.account_id if service else "",
                    bot_id=service.bot_id if service else "",
                    matched=verdict.matched,
                    handled=handled,
                )
            )
        except Exception as e:
            logger.warning("安全事件写入失败: %s", e)

    async def _touch_and_backfill_memory(
        self,
        service_id: str,
        all_facts: list[MemoryFact],
        injected_contents: list[str],
        emb_service,
    ) -> None:
        """后台维护长期记忆（fire-and-forget）。

        1. 刷新本轮被注入事实的 last_referenced_at（使常用记忆保持新鲜）
        2. 为缺失向量的存量事实渐进式回填 embedding（每轮上限 10 条）

        已标失效（invalid_at/expired_at 非空）的事实不再刷新引用时间、不再强化强度：
        否则已失效旧事实会因被注入而“越记越牢”，时间衰减机制被自我强化循环破坏。
        """
        try:
            # 批量更新引用时间（减少 MongoDB RT）；已失效事实排除在外
            if injected_contents:
                invalidated_contents = {
                    f.content for f in all_facts if is_fact_invalidated(f)
                }
                refreshable = [
                    c for c in injected_contents if c not in invalidated_contents
                ]
                if refreshable:
                    await self.memory_repo.update_facts_reference(
                        service_id, refreshable
                    )

            if emb_service.enabled:
                missing = [f for f in all_facts if not f.embedding][:10]
                if missing:
                    vectors = await emb_service.embed_batch(
                        [f.content for f in missing]
                    )
                    for fact, vec in zip(missing, vectors, strict=True):
                        if vec:
                            await self.memory_repo.set_fact_embedding(
                                service_id, fact.content, vec
                            )
        except Exception as e:
            logger.warning("记忆后台维护失败: %s", e)

    async def _complete_with_tools(
        self,
        provider,
        messages: list[dict],
        max_tokens: int,
        service_memory,
    ) -> tuple[str, list[tuple[str, bool]], LLMUsage]:
        """Function Calling 工具循环：允许模型自主检索记忆/请求配图。

        最多 3 轮工具调用；返回 (最终回复文本, 图片提示列表(描述, 是否本人出镜),
        各轮 LLM 调用累计的 token usage)，供调用方按量计费一次性结算。
        """
        from app.llm.tools import ALL_TOOLS, search_facts

        working: list[dict] = list(messages)
        image_specs: list[tuple[str, bool]] = []
        facts = list(service_memory.facts) if service_memory else []
        emb_service = get_embedding_service()
        text = ""
        total_usage = LLMUsage()

        for _ in range(3):
            result = await provider.complete_with_tools(
                working, ALL_TOOLS,
                temperature=settings.chat_temperature,
                max_tokens=max_tokens,
            )
            # 多轮工具循环每轮的 usage 累计，确保计费覆盖全部 API 消耗
            total_usage.prompt_tokens += result.usage.prompt_tokens
            total_usage.completion_tokens += result.usage.completion_tokens
            if not result.tool_calls:
                text = result.content
                break

            # 追加助手的工具调用消息
            working.append({
                "role": "assistant",
                "content": result.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": tc.arguments},
                    }
                    for tc in result.tool_calls
                ],
            })
            # 逐个执行工具并回填结果
            for tc in result.tool_calls:
                try:
                    args = json.loads(tc.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                if tc.name == "retrieve_memory":
                    query = str(args.get("query", ""))
                    q_emb = (
                        await emb_service.embed(query)
                        if emb_service.enabled else []
                    )
                    hits = search_facts(query, facts, q_emb, topk=5)
                    tool_result = "；".join(hits) if hits else "（没有相关记忆）"
                elif tc.name == "generate_image":
                    desc = str(args.get("description", "")).strip()
                    if desc:
                        image_specs.append(
                            (desc, bool(args.get("contains_self", False)))
                        )
                    tool_result = "已安排生成图片"
                else:
                    tool_result = "未知工具"
                working.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_result,
                })
        else:
            # 工具循环用尽仍未产出正文 → 追加一次普通补全兜底（同样累计 usage）
            if not text:
                text, fallback_usage = await provider.complete_with_usage(
                    messages=working,
                    temperature=settings.chat_temperature,
                    max_tokens=max_tokens,
                )
                total_usage.prompt_tokens += fallback_usage.prompt_tokens
                total_usage.completion_tokens += fallback_usage.completion_tokens

        return text, image_specs, total_usage

    @staticmethod
    def _build_image_tasks(
        specs: list[tuple[str, bool]],
        profile: PromptProfileInDB,
        mood: CharacterMood | None = None,
    ) -> list[ImageTask]:
        """将 (描述, 是否本人出镜) 列表组装为结构化图片任务。

        本人出镜时携带角色定妆照（供参考图编辑保持人物一致）；
        同时预置降级描述（外貌文字 + 场景），供纯文生图兜底链路使用。
        心情强烈时追加情绪氛围修饰词，使本人出镜表情与当前心情一致。
        """
        ref_url = getattr(profile, "reference_image_url", "") or ""
        identity = getattr(profile, "identity", None)
        appearance = (getattr(identity, "appearance", "") or "").strip()
        mood_suffix = MoodManager.image_mood_suffix(mood)
        tasks: list[ImageTask] = []
        for prompt, contains_self in specs:
            # 本人出镜且心情强烈时，追加情绪氛围修饰词
            if contains_self and mood_suffix:
                prompt = prompt + mood_suffix
            fallback = prompt
            if contains_self and appearance and appearance.lower() not in prompt.lower():
                # 降级为纯文生图时需补充外貌特征，尽量维持形象稳定
                fallback = f"{appearance}, {prompt}"
            tasks.append(ImageTask(
                prompt=prompt,
                contains_self=contains_self,
                ref_image_url=ref_url if contains_self else "",
                fallback_prompt=fallback,
            ))
        return tasks

    async def _resolve_profile(
        self, profile_id: str, account_id: str
    ) -> Optional[PromptProfileInDB]:
        """
        解析当前角色。

        直接从 service.profile_id 查询对应角色，
        同时校验角色对该账号是否可见。
        若未找到或已禁用，返回 None 由上层提示创建角色。
        """
        if profile_id:
            # 优先按 ObjectId 查询，无效时按 slug 查询
            if ObjectId.is_valid(profile_id):
                profile = await self.profile_repo.find_by_id(profile_id)
            else:
                profile = await self.profile_repo.find_by_slug(profile_id)
            if profile and profile.is_active:
                # 校验可见性
                if profile.visibility == "private" and profile.account_id != account_id:
                    return None
                return profile
        return None

    def _no_service_message(self) -> str:
        return "您尚未绑定对话服务，请联系管理员。"

    def _no_profile_message(self) -> str:
        return "当前尚未配置角色，请先在管理后台「角色管理」中创建角色后再开始对话。"

    @staticmethod
    def _is_uuid(value: str) -> bool:
        """判断字符串是否为 UUID 格式（管理员创建用户时自动生成的占位 platform_id）。"""
        import uuid
        try:
            uuid.UUID(value)
            return True
        except (ValueError, AttributeError):
            return False
