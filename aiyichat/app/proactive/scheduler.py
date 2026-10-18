"""
主动消息调度器。

基于 asyncio 定时任务，周期性检查需要主动触达的对方，
根据策略生成关怀消息并通过对应平台 adapter 发送。
"""

import asyncio
import logging
import random
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from app.config import settings
from app.conversation.time_context import resolve_tz
from app.db.repositories.message_repo import MessageRepository
from app.db.repositories.proactive_repo import ProactiveRecordRepository
from app.db.repositories.service_memory_repo import ServiceMemoryRepository
from app.db.repositories.service_repo import ServiceRepository
from app.db.repositories.identity_repo import IdentityRepository
from app.models.proactive_record import ProactiveRecord
from app.proactive.strategy import (
    ProactiveStage,
    get_pending_stage,
)

logger = logging.getLogger(__name__)

# 检查间隔（秒）：每15分钟扫描一次（更精细的触发判定，配合免打扰时段）
CHECK_INTERVAL_SECONDS = 900

# 同 Bot 服务级冷却（秒）：同一 (对方, Bot) 一小时内只发一条主动消息，
# 避免同 Bot 下多个服务在同轮扫描中秒级连发而撞上 QQ 召回区间上限；
# 阶段策略最小间隔为 4h，冷却不会阻断正常的阶段推进
SEND_COOLDOWN_SECONDS = 3600

# 分享型"创作趣事"的主题类别池（随机抽取提升多样性；严禁涉及时事/新闻）
_SHARE_TRIVIA_CATEGORIES = ("冷知识", "生活小发现", "有趣见闻", "脑洞想法")

# 免打扰时段与对方时区改为运行时配置（settings.proactive_quiet_hour_* /
# default_timezone_offset），后台修改后热生效，无需重启；
# 时区与对话时间感知共用单一配置源，避免两者静默分叉


class ProactiveScheduler:
    """
    主动消息调度器。

    周期性扫描所有活跃服务的对方，根据 last_active_at 和已发送记录
    判断是否需要发送主动消息，并调用对应平台 adapter 执行发送。
    """

    def __init__(self) -> None:
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None
        self._proactive_repo = ProactiveRecordRepository()
        self._service_repo = ServiceRepository()
        self._identity_repo = IdentityRepository()
        self._msg_repo = MessageRepository()
        self._memory_repo = ServiceMemoryRepository()
        # 平台管理器引用（启动时注入）
        self._qq_manager = None
        self._wechat_manager = None
        # 最近一次发送失败的详情（由 _send_* 写入，供失败记录留痕）
        self._last_send_detail: str = ""

    def set_managers(self, qq_manager, wechat_manager) -> None:
        """注入平台Bot管理器引用（在 main.py lifespan 中调用）。"""
        self._qq_manager = qq_manager
        self._wechat_manager = wechat_manager

    async def start(self) -> None:
        """启动调度器后台循环。"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("ProactiveScheduler 已启动，检查间隔 %ds", CHECK_INTERVAL_SECONDS)

    async def stop(self) -> None:
        """停止调度器。"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("ProactiveScheduler 已停止")

    async def _loop(self) -> None:
        """主循环：周期性执行扫描（启动后立即扫描一次，再进入定时循环）。"""
        while self._running:
            # 先执行扫描，再等待，确保启动后即时触发首轮判定
            try:
                await self._scan_and_send()
            except Exception as e:
                logger.error("ProactiveScheduler 扫描异常: %s", e, exc_info=True)
                # 错误追踪：聚合相同错误，避免日志洪水
                from app.utils.error_tracker import track_error

                await track_error(e, context={"component": "proactive_scheduler"})
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

    def _is_quiet_hours(self, service: object = None, identity: object = None) -> bool:
        """判断当前是否处于免打扰时段（**对方本地时区**，配置驱动，默认 22:00-08:00）。

        时区走解析链 service.timezone → user.timezone → 全局默认偏移：
        用全局偏移判"深夜别打扰"，会给海外或上夜班的人在当地凌晨发消息，
        既扰人，也暴露角色并不关心对方身在何处。

        处于该时段时本轮跳过发送，等待下一轮扫描；白天恢复后自动触发。
        start == end 视为未启用免打扰。
        """
        start = settings.proactive_quiet_hour_start
        end = settings.proactive_quiet_hour_end
        if start == end:
            return False
        tz, _label = resolve_tz(
            getattr(service, "timezone", "") or "",
            getattr(identity, "timezone", "") or "",
        )
        hour = datetime.now(tz).hour
        if start > end:
            # 跨午夜场景（如 22~8）：>=start 或 <end 都视为免打扰
            return hour >= start or hour < end
        # 同日场景（如 1~6）
        return start <= hour < end

    async def _scan_and_send(self) -> None:
        """
        扫描所有活跃服务，检查每个对方是否需要主动消息。
        """
        # 主动关怀总开关：关闭时直接跳过整轮扫描（异常时可一键停用）
        if not settings.proactive_enabled:
            logger.debug("主动关怀总开关已关闭，跳过本轮扫描")
            return

        # 获取所有活跃服务
        services = await self._service_repo.list_all_active()
        if not services:
            return

        now = datetime.now(timezone.utc)
        # 只关注30天内活跃过的对方（超过30天不再发送）
        cutoff = now - timedelta(days=30)

        logger.debug(
            "开始扫描主动消息，活跃服务数=%d, cutoff=%s",
            len(services), cutoff.isoformat(),
        )

        for service in services:
            try:
                await self._process_service(service, cutoff)
            except Exception as e:
                logger.error(
                    "处理服务 %s 主动消息失败: %s",
                    service.id, e,
                )

    async def _process_service(self, service, cutoff: datetime) -> None:
        """处理单个服务的主动消息逻辑。"""
        # 获取对方信息
        identity = await self._identity_repo.find_by_id(service.identity_id)
        if identity is None:
            logger.debug("跳过：对方不存在 service_id=%s, identity_id=%s", service.id, service.identity_id)
            return
        # 跳过被禁用的对方
        if identity.is_blocked:
            logger.debug("跳过被禁用对方: identity_id=%s", identity.id)
            return
        # 跳过没有绑定真实平台ID的对方（UUID格式 = 未绑定）
        if self._is_uuid(identity.platform_id):
            logger.debug("跳过未绑定对方: identity_id=%s", identity.id)
            return

        # 主动消息仅支持 QQ/微信平台（web 等无主动推送通道，ProactiveRecord 亦不接受）
        if identity.platform not in ("qq", "wechat"):
            logger.debug(
                "跳过非推送平台对方: identity_id=%s, platform=%s",
                identity.id, identity.platform,
            )
            return

        # 发送目标按 Bot 路由：QQ openid 按 appid 生成，同一对方在不同 Bot 下不同，
        # 直接用 user.platform_id（随最近互动的 Bot 被覆盖）会导致其他 Bot 发送被平台拒绝
        target_id = self._resolve_platform_id(identity, service.bot_id)

        # 同 Bot 服务级冷却：一小时内该 (对方, Bot) 已发过主动消息则本轮跳过，
        # 防止同 Bot 下多个服务秒级连发；不同 Bot 的服务互不影响，均可独立触达
        last_sent = await self._proactive_repo.get_latest_sent(
            identity_id=identity.id, bot_id=service.bot_id
        )
        if last_sent is not None and last_sent.sent_at is not None:
            since_sent = (
                datetime.now(timezone.utc)
                - self._ensure_aware(last_sent.sent_at)
            ).total_seconds()
            if since_sent < SEND_COOLDOWN_SECONDS:
                logger.debug(
                    "跳过：同 Bot 冷却期内 identity_id=%s, bot_id=%s, elapsed=%.0fs",
                    identity.id, service.bot_id, since_sent,
                )
                return

        # 事件驱动跟进（优先于阶段策略），由开关控制
        if settings.proactive_event_enabled:
            if await self._process_pending_events(service, identity):
                return

        # 统一时区工具：MongoDB 中的时间可能是 naive（无时区），需转为 aware
        def _to_aware(dt: datetime | None) -> datetime:
            if dt is None:
                return datetime.min.replace(tzinfo=timezone.utc)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt

        # 计时基准：取对方维度和服务维度的最后活跃时间的较大值
        # 防止「对方在另一个服务里刚刚活跃，本服务却被误判为长时间未活跃」的情况
        identity_last_active = _to_aware(identity.last_active_at)
        service_last_message = _to_aware(getattr(service, "last_message_at", None))
        last_active = max(identity_last_active, service_last_message)

        # 跳过超过30天未活跃的对方
        if last_active < cutoff:
            logger.debug(
                "跳过超期对方: identity_id=%s, last_active=%s, cutoff=%s",
                identity.id, last_active.isoformat(), cutoff.isoformat(),
            )
            return
        # 跳过最近刚活跃的对方（至少1小时后才发第一条，匹配QQ召回消息当天周期）
        now = datetime.now(timezone.utc)
        if (now - last_active).total_seconds() < 1 * 3600:
            logger.debug(
                "跳过刚活跃对方: identity_id=%s, last_active=%s, elapsed=%.2fh",
                identity.id, last_active.isoformat(),
                (now - last_active).total_seconds() / 3600,
            )
            return

        # 查询该对方已尝试的阶段（成功发送 或 失败次数达上限）
        # 失败达上限即抑制后续重试，避免每轮扫描都重新调用 LLM 生成内容而耗尽额度
        attempted_stages = await self._proactive_repo.get_attempted_stages(
            identity_id=identity.id,
            bot_id=service.bot_id,
            since=last_active,
            max_retries=settings.proactive_max_retries,
        )

        # 免打扰时段控制：提前到阶段判定之前，避免无谓的阶段计算
        # 处于深夜时段则跳过本轮，等待下一轮扫描（白天后自动触发）
        if self._is_quiet_hours(service, identity):
            logger.info("免打扰时段，跳过本轮: user=%s", identity.id)
            return

        # 判断当前应发送的阶段
        stage = get_pending_stage(
            last_active_at=last_active,
            sent_stages=attempted_stages,
            platform=identity.platform,
        )
        if stage is None:
            logger.debug(
                "跳过：无待发送阶段 identity_id=%s, attempted_stages=%s",
                identity.id, attempted_stages,
            )
            return

        # 月配额硬上限校验：防止异常情况下超发（QQ 官方约束每月四条）
        monthly_sent = await self._proactive_repo.count_sent_this_month(
            identity_id=identity.id, bot_id=service.bot_id, platform=identity.platform
        )
        if monthly_sent >= settings.proactive_monthly_limit:
            logger.info(
                "跳过：已达每月主动消息上限 identity_id=%s, sent=%d, limit=%d",
                identity.id, monthly_sent, settings.proactive_monthly_limit,
            )
            return

        # 发送通道预检：在调用 LLM 生成内容之前判断能否发送，
        # 通道不可用（QQ限流退避 / 微信无context_token）时直接跳过，不消耗额度
        if not self._can_send(identity.platform, service.bot_id, target_id):
            logger.info(
                "跳过：发送通道不可用（预检未通过）identity_id=%s, platform=%s, stage=%d",
                identity.id, identity.platform, stage.stage,
            )
            return

        # 分享型骰子：召回触发点按可调概率用"时事/趣事分享"替代关怀型内容，
        # 不新增发送频次（月配额/免打扰/通道预检已在此前生效）
        content_type: Literal["care", "news", "trivia"] = "care"
        if (
            settings.proactive_share_enabled
            and random.random() < settings.proactive_share_ratio
        ):
            content_type = (
                "news"
                if random.random() < settings.proactive_share_news_ratio
                else "trivia"
            )

        # 先创建 pending 记录（生成或发送失败也留痕，用于统计与重试熔断）
        record = ProactiveRecord(
            identity_id=identity.id,
            platform_openid=target_id,
            platform=identity.platform,
            bot_id=service.bot_id,
            stage=stage.stage,
            content_type=content_type,
        )
        await self._proactive_repo.create(record)

        # 生成消息内容（此步消耗 LLM 额度，故置于总开关/配额/预检之后）
        referenced_event = ""
        if content_type == "care":
            content, referenced_event = await self._generate_content(
                identity=identity,
                service=service,
                stage=stage,
            )
        else:
            content = await self._generate_share_content(
                identity=identity,
                service=service,
                share_kind=content_type,
            )
        if not content:
            logger.debug(
                "跳过：消息内容生成为空 identity_id=%s, stage=%d",
                identity.id, stage.stage,
            )
            await self._proactive_repo.mark_failed(record.id, "内容生成为空")
            return

        # 执行发送
        success = await self._send_message(
            platform=identity.platform,
            bot_id=service.bot_id,
            platform_id=target_id,
            content=content,
        )

        # 更新记录状态
        if success:
            await self._proactive_repo.mark_sent(record.id, content)
            # 追问式关怀：文案引用了待跟进事件时，标记已跟进避免重复追问
            if referenced_event:
                await self._memory_repo.mark_event_followed_up(
                    service.id, referenced_event
                )
            logger.info(
                "主动消息发送成功: user=%s, stage=%d, platform=%s, type=%s",
                identity.id, stage.stage, identity.platform, content_type,
            )
        else:
            await self._proactive_repo.mark_failed(
                record.id, self._last_send_detail or "发送接口返回失败"
            )

    async def _generate_content(
        self, identity, service, stage: ProactiveStage
    ) -> tuple[str, str]:
        """
        通过 LLM 生成主动消息内容。

        使用对话引擎的 Prompt 组装能力，结合角色设定、对方上下文和情绪感知；
        存在未跟进的待办事件时优先追问（"你上次说的 X 后来怎么样了"），
        提升陪伴真实感。

        Returns:
            (消息内容, 被引用的待跟进事件内容；未引用时为空串)
        """
        try:
            from app.conversation.context_manager import ContextManager
            from app.conversation.response_processor import ResponseProcessor
            from app.db.repositories.bot_config_repo import BotConfigRepository
            from app.db.repositories.conversation_repo import ConversationRepository
            from app.db.repositories.profile_repo import ProfileRepository
            from app.db.repositories.service_memory_repo import ServiceMemoryRepository
            from app.llm.prompt_assembler import PromptAssembler
            from app.llm.provider_factory import get_llm_provider

            # 获取 Bot 和 Profile 信息
            bot_repo = BotConfigRepository()
            bot = await bot_repo.find_by_id(service.bot_id)
            if not bot:
                return "", ""

            profile_repo = ProfileRepository()
            # profile_id 可能是 slug 或 ObjectId，优先按 slug 查找
            profile = await profile_repo.find_by_slug(service.profile_id)
            if not profile:
                try:
                    profile = await profile_repo.find_by_id(service.profile_id)
                except Exception:
                    pass
            if not profile:
                return "", ""

            # 获取最近对话上下文（用于生成连贯的主动消息；共享组按组 key）
            conv_repo = ConversationRepository()
            conversation = await conv_repo.find_latest_by_service(
                service.context_key
            )

            history: list = []
            recent_emotion: str | None = None
            if conversation:
                ctx_mgr = ContextManager()
                history, _ = await ctx_mgr.get_context(
                    conversation.id, conversation.summary
                )
                # 推断对方近期情绪趋势
                recent_emotion = await self._infer_recent_emotion(conversation.id)

            # 获取长期记忆
            memory_facts: list[str] = []
            memory_repo = ServiceMemoryRepository()
            service_memory = await memory_repo.find_by_service(
                service.context_key
            )
            if service_memory:
                sorted_facts = sorted(
                    service_memory.facts,
                    key=lambda f: f.last_referenced_at,
                    reverse=True,
                )
                memory_facts = [f.content for f in sorted_facts[:5]]

            # 追问式关怀：存在未跟进的待办事件时，优先引用最近一条具体追问
            referenced_event = ""
            if service_memory and service_memory.pending_events:
                pending = [
                    e for e in service_memory.pending_events if not e.followed_up
                ]
                if pending:
                    referenced_event = pending[-1].content

            # 根据对方情绪调整内容提示
            content_hint = stage.content_hint
            if recent_emotion and stage.emotion_hints:
                content_hint = stage.emotion_hints.get(recent_emotion, stage.content_hint)

            # 获取最近一次主动消息内容用于去重
            last_record = await self._proactive_repo.get_latest_sent(
                identity_id=identity.id, bot_id=service.bot_id
            )
            last_content = last_record.content if last_record else ""

            # 获取对方本地时间与时间感知提示（用于生成与时间匹配的内容）
            identity_now, time_context, time_hint = self._time_hint(service, identity)

            # 组装提示词：指示 LLM 生成主动关怀消息（注入时间感知）
            proactive_instruction = (
                f"现在你需要主动给对方发一条消息。"
                f"当前时间是{time_context}（{identity_now.strftime('%H:%M')}），"
                f"{time_hint}。"
                f"要求：{content_hint}。"
                f"注意：消息要自然、简短（不超过50字），符合你的角色人设，"
                f"不要提及'主动消息'或'定时发送'等概念。"
            )
            if referenced_event:
                proactive_instruction += (
                    f"\n对方之前提到过：「{referenced_event}」，"
                    f"请优先自然地关心这件事的进展或结果，体现你记得并在意。"
                )
            if last_content:
                proactive_instruction += (
                    f"\n避免重复上次的问候方式：「{last_content[:30]}」"
                )

            assembler = PromptAssembler()
            messages = assembler.assemble(
                profile=profile,
                history=history[-6:],  # 只取最近几轮作为上下文
                identity_input=proactive_instruction,
                emotion_hint="",
                identity=identity,
                memory_facts=memory_facts[:5],
                current_topic="",
                gap_mode="proactive",
            )

            # 调用 LLM（提高 temperature 增加多样性，非实时场景使用较长超时）
            provider = get_llm_provider("proactive")
            response = await provider.complete(
                messages,
                temperature=settings.proactive_temperature,
                max_tokens=settings.proactive_max_tokens,
            )
            response = ResponseProcessor.strip_time_tags(response)
            # 截断过长的回复
            if len(response) > 100:
                response = response[:100]
            return response.strip(), referenced_event

        except Exception as e:
            logger.error("生成主动消息内容失败: %s", e, exc_info=True)
            # 错误追踪：聚合相同错误，避免日志洪水
            from app.utils.error_tracker import track_error

            await track_error(e, context={
                "component": "proactive_content",
                "identity_id": identity.id,
                "service_id": service.id,
            })
            return "", ""

    def _time_hint(
        self, service: object = None, identity: object = None
    ) -> tuple[datetime, str, str]:
        """获取对方本地时间与时间感知提示（关怀型/分享型共用）。

        时区走解析链 service.timezone → user.timezone → 全局默认偏移，
        与免打扰判定用同一个时区，避免出现"按 UTC+8 判定不深夜、
        却按对方真实时区说这么晚还没睡"的自相矛盾。

        Returns:
            (对方本地当前时间, 时间段词汇, 该时间段的生成提示)
        """
        tz, _label = resolve_tz(
            getattr(service, "timezone", "") or "",
            getattr(identity, "timezone", "") or "",
        )
        identity_now = datetime.now(tz)
        current_hour = identity_now.hour
        if 0 <= current_hour < 5:
            return identity_now, "凌晨", "可以温柔提醒对方早点休息"
        if 5 <= current_hour < 9:
            return identity_now, "清晨", "可以问候早安、关心对方是否睡得好、今天有什么计划"
        if 9 <= current_hour < 12:
            return identity_now, "上午", "可以关心对方上午的工作/学习状态"
        if 12 <= current_hour < 14:
            return identity_now, "中午", "可以关心对方吃午饭了吗、午休情况"
        if 14 <= current_hour < 18:
            return identity_now, "下午", "可以关心对方下午的工作进展、是否累了需要休息"
        if 18 <= current_hour < 22:
            return identity_now, "晚上", "可以关心对方晚饭、今天的收获、放松休息"
        # 22-24 深夜（兜底，正常不会触发）
        return identity_now, "深夜", "可以温柔提醒对方早点休息"

    async def _generate_share_content(
        self, identity, service, share_kind: str
    ) -> str:
        """生成"分享型"主动消息内容（联网时事 / 创作趣事）。

        与关怀型消息共用上下文/记忆/时间感知/去重组装逻辑；
        news 类开启联网搜索获取当日热点，用角色口吻改写；
        trivia 类随机抽取主题类别，明确禁止涉及时事、新闻、真实人物事件。
        news 类调用失败或结果为空时自动降级为 trivia 类（避免编造时事）。

        Args:
            share_kind: "news"（联网时事）或 "trivia"（创作趣事）

        Returns:
            消息内容，失败时为空串
        """
        try:
            from app.conversation.context_manager import ContextManager
            from app.conversation.response_processor import ResponseProcessor
            from app.db.repositories.bot_config_repo import BotConfigRepository
            from app.db.repositories.conversation_repo import ConversationRepository
            from app.db.repositories.profile_repo import ProfileRepository
            from app.db.repositories.service_memory_repo import ServiceMemoryRepository
            from app.llm.prompt_assembler import PromptAssembler
            from app.llm.provider_factory import get_llm_provider

            bot_repo = BotConfigRepository()
            bot = await bot_repo.find_by_id(service.bot_id)
            if not bot:
                return ""

            profile_repo = ProfileRepository()
            # profile_id 可能是 slug 或 ObjectId，优先按 slug 查找
            profile = await profile_repo.find_by_slug(service.profile_id)
            if not profile:
                try:
                    profile = await profile_repo.find_by_id(service.profile_id)
                except Exception:
                    pass
            if not profile:
                return ""

            # 最近对话上下文（让分享贴合对话语境；共享组按组 key）
            conv_repo = ConversationRepository()
            conversation = await conv_repo.find_latest_by_service(
                service.context_key
            )
            history: list = []
            if conversation:
                ctx_mgr = ContextManager()
                history, _ = await ctx_mgr.get_context(
                    conversation.id, conversation.summary
                )

            # 长期记忆（让分享方式更贴合对方）
            memory_facts: list[str] = []
            service_memory = await ServiceMemoryRepository().find_by_service(
                service.context_key
            )
            if service_memory:
                sorted_facts = sorted(
                    service_memory.facts,
                    key=lambda f: f.last_referenced_at,
                    reverse=True,
                )
                memory_facts = [f.content for f in sorted_facts[:5]]

            # 最近一次主动消息内容用于去重
            last_record = await self._proactive_repo.get_latest_sent(
                identity_id=identity.id, bot_id=service.bot_id
            )
            last_content = last_record.content if last_record else ""

            identity_now, time_context, _ = self._time_hint(service, identity)

            # 按类型构造内容指令：news=联网热点改写，trivia=创作普适趣事（禁写时事新闻）
            if share_kind == "news":
                share_instruction = (
                    "你随手上网看了看今天的热点话题和新闻，"
                    "挑一条有意思的、温暖的或者好玩的，用你自己的口吻分享给对方，"
                    "像朋友随手转发时配一句话那样。"
                    "要求：基于你搜到的内容改写，不要编造具体细节；"
                    "可以自然地在结尾带一句轻松的互动（比如“你看到没”）。"
                )
            else:
                category = random.choice(_SHARE_TRIVIA_CATEGORIES)
                share_instruction = (
                    f"你想跟对方分享一个「{category}」。"
                    f"重要：严禁涉及任何时事、新闻、真实人物事件，"
                    f"只分享普适性的趣事/见闻。"
                    f"可以自然地问问对方怎么看。"
                )

            proactive_instruction = (
                f"{share_instruction}"
                f"现在是{time_context}（{identity_now.strftime('%H:%M')}），语气要贴合时间段。"
                f"消息要自然、简短（不超过50字），符合你的角色人设，"
                f"不要提及'主动消息''定时发送''搜索'等概念。"
            )
            if last_content:
                proactive_instruction += (
                    f"\n避免和上次雷同：「{last_content[:30]}」"
                )

            assembler = PromptAssembler()
            messages = assembler.assemble(
                profile=profile,
                history=history[-6:],  # 只取最近几轮作为上下文
                identity_input=proactive_instruction,
                emotion_hint="",
                identity=identity,
                memory_facts=memory_facts[:5],
                current_topic="",
                gap_mode="proactive",
            )

            provider = get_llm_provider("proactive")
            try:
                response = await provider.complete(
                    messages,
                    temperature=settings.proactive_temperature,
                    max_tokens=settings.proactive_max_tokens,
                    web_search=(share_kind == "news"),
                )
            except Exception as e:
                if share_kind == "news":
                    # 联网搜索失败降级为趣事，保证触达率（避免编造时事）
                    logger.warning("分享型联网搜索调用失败，降级为趣事: %s", e)
                    return await self._generate_share_content(
                        identity, service, "trivia"
                    )
                raise
            response = ResponseProcessor.strip_time_tags(response).strip()
            if not response and share_kind == "news":
                logger.warning(
                    "分享型时事内容为空，降级为趣事: user=%s", identity.id
                )
                return await self._generate_share_content(
                    identity, service, "trivia"
                )
            # 截断过长的回复
            if len(response) > 100:
                response = response[:100]
            return response.strip()

        except Exception as e:
            logger.error("生成分享型消息内容失败: %s", e, exc_info=True)
            # 错误追踪：聚合相同错误，避免日志洪水
            from app.utils.error_tracker import track_error

            await track_error(e, context={
                "component": "proactive_share",
                "identity_id": identity.id,
                "service_id": service.id,
            })
            return ""

    async def _process_pending_events(self, service, identity) -> bool:
        """检查并处理到期的待跟进事件，返回是否已发送事件消息。"""
        now = datetime.now(timezone.utc)
        memory = await self._memory_repo.find_by_service(service.context_key)
        if memory is None or not memory.pending_events:
            return False

        # 找出已到期且未跟进的事件（排除已过期：due_at 早于 now - grace 的事件不再跟进）
        grace = timedelta(hours=24)  # 宽限期：事件过期 24 小时内仍跟进，超出则放弃
        due_events = [
            e for e in memory.pending_events
            if not e.followed_up
            and self._ensure_aware(e.due_at) <= now
            and self._ensure_aware(e.due_at) >= now - grace
        ]
        if not due_events:
            return False

        # 免打扰时段：等待下一轮
        if self._is_quiet_hours(service, identity):
            logger.info("免打扰时段，延迟事件跟进: user=%s", identity.id)
            return False

        event = due_events[0]

        # 发送目标按 Bot 路由（同阶段策略路径）
        target_id = self._resolve_platform_id(identity, service.bot_id)

        # 发送通道预检：通道不可用时跳过本轮（返回 True 不再叠加阶段策略），不消耗额度
        if not self._can_send(identity.platform, service.bot_id, target_id):
            logger.info(
                "跳过事件跟进：发送通道不可用（预检未通过）user=%s",
                identity.id,
            )
            return True

        # 先创建 pending 记录（生成或发送失败也留痕）
        record = ProactiveRecord(
            identity_id=identity.id,
            platform_openid=target_id,
            platform=identity.platform,
            bot_id=service.bot_id,
            stage=0,  # 0 表示事件驱动（非阶段策略）
        )
        await self._proactive_repo.create(record)

        # 生成事件跟进内容（此步消耗 LLM 额度，故置于预检之后）
        content = await self._generate_event_content(
            identity, service, event.content
        )
        if not content:
            await self._proactive_repo.mark_failed(record.id, "内容生成为空")
            return True

        success = await self._send_message(
            platform=identity.platform,
            bot_id=service.bot_id,
            platform_id=target_id,
            content=content,
        )
        if success:
            await self._proactive_repo.mark_sent(record.id, content)
            await self._memory_repo.mark_event_followed_up(
                service.context_key, event.content
            )
            logger.info(
                "事件跟进发送成功: user=%s, event='%s'",
                identity.id, event.content[:20],
            )
        else:
            await self._proactive_repo.mark_failed(
                record.id, self._last_send_detail or "发送接口返回失败"
            )
        return True

    @staticmethod
    def _ensure_aware(dt: datetime) -> datetime:
        """将可能为 naive 的时间转为 UTC aware。"""
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    async def _generate_event_content(
        self, identity, service, event_content: str
    ) -> str:
        """为到期事件生成主动跟进消息内容。"""
        try:
            from app.conversation.context_manager import ContextManager
            from app.conversation.response_processor import ResponseProcessor
            from app.db.repositories.bot_config_repo import BotConfigRepository
            from app.db.repositories.conversation_repo import ConversationRepository
            from app.db.repositories.profile_repo import ProfileRepository
            from app.llm.prompt_assembler import PromptAssembler
            from app.llm.provider_factory import get_llm_provider

            bot_repo = BotConfigRepository()
            bot = await bot_repo.find_by_id(service.bot_id)
            if not bot:
                return ""

            profile_repo = ProfileRepository()
            profile = await profile_repo.find_by_slug(service.profile_id)
            if not profile:
                try:
                    profile = await profile_repo.find_by_id(service.profile_id)
                except Exception:
                    pass
            if not profile:
                return ""

            conv_repo = ConversationRepository()
            conversation = await conv_repo.find_latest_by_service(
                service.context_key
            )
            history: list = []
            if conversation:
                ctx_mgr = ContextManager()
                history, _ = await ctx_mgr.get_context(
                    conversation.id, conversation.summary
                )

            instruction = (
                f"你之前记得对方提到过：「{event_content}」。"
                f"现在你想主动关心一下这件事的进展或结果，"
                f"自然、简短地发一条消息（不超过50字），符合你的人设，"
                f"体现你记得并在意这件事，不要提及'定时''系统'等概念。"
            )

            assembler = PromptAssembler()
            messages = assembler.assemble(
                profile=profile,
                history=history[-6:],
                identity_input=instruction,
                emotion_hint="",
                identity=identity,
                current_topic="",
                gap_mode="proactive",
            )
            provider = get_llm_provider("proactive")
            response = await provider.complete(
                messages,
                temperature=settings.proactive_temperature,
                max_tokens=settings.proactive_max_tokens,
            )
            response = ResponseProcessor.strip_time_tags(response)
            if len(response) > 100:
                response = response[:100]
            return response.strip()
        except Exception as e:
            logger.error("生成事件跟进内容失败: %s", e, exc_info=True)
            return ""

    async def _send_message(
        self,
        platform: str,
        bot_id: str,
        platform_id: str,
        content: str,
    ) -> bool:
        """通过对应平台的 adapter 发送消息。

        失败时将平台返回的真实原因写入 self._last_send_detail，
        供调用方记录到 ProactiveRecord.error_msg（便于后台诊断）。
        """
        self._last_send_detail = ""
        if platform == "qq":
            return await self._send_qq(bot_id, platform_id, content)
        elif platform == "wechat":
            return await self._send_wechat(bot_id, platform_id, content)
        else:
            logger.warning("不支持的平台主动消息: %s", platform)
            self._last_send_detail = f"不支持的推送平台: {platform}"
            return False

    @staticmethod
    def _resolve_platform_id(identity, bot_id: str) -> str:
        """解析对方在指定 Bot 下的发送目标 openid。

        QQ 的 openid 按 appid 生成，同一对方在不同 Bot 下并不相同；
        优先取 bot_openids 中该 Bot 的映射，缺失时回退 user.platform_id
        （存量数据与单 Bot 场景兜底）。
        """
        mapping = getattr(identity, "bot_openids", None) or {}
        return mapping.get(bot_id) or identity.platform_id

    def _can_send(
        self, platform: str, bot_id: str, platform_id: str
    ) -> bool:
        """发送前的轻量通道预检（不消耗 LLM 额度）。

        在调用 LLM 生成内容之前判断目标通道是否可用：
        - QQ：处于限流退避期则不可发；
        - 微信：无缓存的 context_token 则不可发（服务重启后常见）。
        客户端不存在或未实现预检方法时保守返回 True，交由实际发送环节处理。
        """
        try:
            if platform == "qq":
                if self._qq_manager is None:
                    return False
                client = self._qq_manager.get_client(bot_id)
            elif platform == "wechat":
                if self._wechat_manager is None:
                    return False
                client = self._wechat_manager.get_client(bot_id)
            else:
                return False
            if client is None:
                return False
            checker = getattr(client, "can_send_proactive", None)
            if checker is None:
                return True
            # 群聊对方：platform_id 格式为 group_id::member_openid，
            # 限流预检使用 member_openid 部分
            check_id = platform_id
            if "::" in platform_id:
                check_id = platform_id.split("::", 1)[1]
            return bool(checker(check_id))
        except Exception as e:
            logger.warning("发送通道预检异常（默认放行）: %s", e)
            return True

    async def _infer_recent_emotion(self, conversation_id: str) -> str | None:
        """从最近消息中推断对方情绪趋势。

        Args:
            conversation_id: 对话ID

        Returns:
            最近出现频率最高的情绪标签，或None
        """
        messages = await self._msg_repo.get_recent(conversation_id, 10)
        identity_messages = [m for m in messages if m.role == "user"]
        emotions = [m.emotion_tag for m in identity_messages if m.emotion_tag]
        if not emotions:
            return None
        # 取最近出现频率最高的情绪
        return Counter(emotions).most_common(1)[0][0]

    async def _send_qq(
        self, bot_id: str, user_openid: str, content: str
    ) -> bool:
        """通过QQ Bot发送互动召回消息。

        群聊对方的 platform_id 格式为 group_id::member_openid，
        需要拆分后走群聊主动消息通道（非 is_wakeup）。
        """
        if self._qq_manager is None:
            logger.error("QQ BotManager 未注入")
            self._last_send_detail = "QQ BotManager 未注入"
            return False
        client = self._qq_manager.get_client(bot_id)
        if client is None:
            logger.error("QQ Bot %s 客户端未找到", bot_id)
            self._last_send_detail = f"QQ Bot 客户端未找到（未连接或已下线）: {bot_id}"
            return False

        # 群聊对方：platform_id 格式为 group_id::member_openid
        if "::" in user_openid:
            group_id, member_openid = user_openid.split("::", 1)
            ok = await client.send_proactive(
                identity_id=member_openid,
                content=content,
                is_group=True,
                group_id=group_id,
            )
        else:
            ok = await client.send_proactive(
                identity_id=user_openid, content=content
            )
        if not ok:
            # 透传适配器记录的 QQ 真实错误码/消息（无则留空，由调用方兜底文案）
            self._last_send_detail = getattr(client, "last_send_error", "") or ""
        return ok

    async def _send_wechat(
        self, bot_id: str, identity_id: str, content: str
    ) -> bool:
        """通过微信Bot发送主动消息（依赖缓存的 context_token）。"""
        if self._wechat_manager is None:
            logger.error("WeChat BotManager 未注入")
            self._last_send_detail = "WeChat BotManager 未注入"
            return False
        client = self._wechat_manager.get_client(bot_id)
        if client is None:
            logger.error("WeChat Bot %s 客户端未找到", bot_id)
            self._last_send_detail = (
                f"WeChat Bot 客户端未找到（未连接或已下线）: {bot_id}"
            )
            return False
        ok = await client.send_proactive(identity_id=identity_id, content=content)
        if not ok:
            self._last_send_detail = getattr(client, "last_send_error", "") or ""
        return ok

    @staticmethod
    def _is_uuid(value: str) -> bool:
        """判断字符串是否为UUID格式（未绑定的预创建对方使用UUID作为platform_id）。"""
        import re
        return bool(
            re.match(
                r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
                value,
            )
        )


# 全局调度器实例（单例）
_scheduler: Optional[ProactiveScheduler] = None


def get_proactive_scheduler() -> ProactiveScheduler:
    """获取全局调度器实例。"""
    global _scheduler
    if _scheduler is None:
        _scheduler = ProactiveScheduler()
    return _scheduler
