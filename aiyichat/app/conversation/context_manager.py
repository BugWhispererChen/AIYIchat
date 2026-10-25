import logging
import time
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.db.repositories.conversation_repo import ConversationRepository
from app.db.repositories.message_repo import MessageRepository
from app.llm.provider_factory import get_llm_provider
from app.models.message import MessageInDB

logger = logging.getLogger(__name__)

# tiktoken 编码器缓存（用于 token 预算估算；非 OpenAI 模型亦可作近似代理）。
# 首次使用会联网下载 BPE 文件（境内实测 80s+，且容器重启后 /tmp 缓存清空），
# 因此只在启动时后台预热，请求路径**绝不**现场等下载：未就绪就按字符近似。
_encoder = None
_encoder_failed = False


def _ensure_aware(dt: datetime) -> datetime:
    """将可能为 naive 的时间转为 UTC aware（存量数据可能无时区信息）。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def warm_up_encoder() -> bool:
    """后台预热 tiktoken 编码器；失败则永久降级为字符近似，不再反复重试。"""
    global _encoder, _encoder_failed
    if _encoder is not None:
        return True
    if _encoder_failed:
        return False
    started = time.monotonic()
    try:
        import tiktoken

        _encoder = tiktoken.get_encoding("cl100k_base")
        logger.info(
            "tiktoken 编码器预热完成: %.1fs", time.monotonic() - started
        )
        return True
    except Exception as e:
        _encoder_failed = True
        logger.warning(
            "tiktoken 编码器不可用（%.1fs），token 预算改用字符近似: %s",
            time.monotonic() - started, e,
        )
        return False


def _count_tokens(text: str) -> int:
    """估算文本 token 数。编码器未就绪时退化为字符数近似。"""
    if not text:
        return 0
    if _encoder is not None:
        return len(_encoder.encode(text))
    # 退化近似：中文约 1.5 字符/token
    return max(1, int(len(text) / 1.5))


class ContextManager:
    """
    对话上下文管理器。

    职责：
    1. 维护自适应滑动窗口，根据情绪强度动态调整窗口大小
    2. 增量摘要：只处理新增消息并与旧摘要合并，减少重复计算
    3. 摘要生成移出主链路，由调用方以后台任务方式触发（fire-and-forget）
    """

    def __init__(self) -> None:
        self.message_repo = MessageRepository()
        self.conversation_repo = ConversationRepository()

    def _compute_window_size(self, emotion_intensity: float) -> int:
        """根据情绪强度计算自适应窗口大小（情绪强烈时扩大以保留更多上下文）。"""
        window_size: int = settings.context_window_size  # 默认20
        if emotion_intensity >= 0.7:
            window_size = int(settings.context_window_size * 1.5)  # 30
        return window_size

    async def get_context(
        self,
        conversation_id: str,
        current_summary: str | None = None,
        emotion_intensity: float = 0.0,
    ) -> tuple[list[MessageInDB], str | None]:
        """
        获取对话上下文（自适应窗口 + 已有摘要）。

        仅做窗口计算与消息读取，快速返回；摘要生成不在此阻塞，
        由调用方在本轮回复完成后通过 schedule_summary_if_needed 后台触发。

        Args:
            conversation_id: 对话ID
            current_summary: 当前已有的摘要文本
            emotion_intensity: 情绪强度(0.0~1.0)，强烈时扩大上下文窗口

        Returns:
            (recent_messages, summary)
        """
        window_size = self._compute_window_size(emotion_intensity)
        recent = await self.message_repo.get_recent(conversation_id, window_size)
        # 在窗口基础上再施加 token 预算约束，避免长消息撑爆上下文
        recent = self._trim_to_token_budget(recent, current_summary)
        return recent, current_summary

    def _trim_to_token_budget(
        self,
        messages: list[MessageInDB],
        summary: str | None,
    ) -> list[MessageInDB]:
        """按 token 预算从旧到新裁剪历史消息，保留最近的对话。

        预算涵盖历史消息与已有摘要；context_token_budget<=0 时不裁剪。
        始终至少保留最近 1 条消息，避免空上下文。
        """
        budget = settings.context_token_budget
        if budget <= 0 or not messages:
            return messages
        used = _count_tokens(summary or "")
        kept: list[MessageInDB] = []
        for msg in reversed(messages):  # 从最新往回累加
            cost = _count_tokens(getattr(msg, "content", "") or "")
            if kept and used + cost > budget:
                break
            used += cost
            kept.append(msg)
        kept.reverse()
        return kept

    async def schedule_summary_if_needed(
        self,
        conversation_id: str,
        current_summary: str | None,
        summary_message_count: int,
        emotion_intensity: float = 0.0,
    ) -> None:
        """
        判定是否需要增量摘要，需要时生成并持久化（设计为后台 fire-and-forget 调用）。

        触发条件：总消息数超过 summary_trigger_count，
        且待摘要消息数（总数 - 窗口）超过上次摘要已覆盖数。

        Args:
            conversation_id: 对话ID
            current_summary: 当前已有摘要
            summary_message_count: 当前摘要已覆盖的消息数量
            emotion_intensity: 情绪强度（用于与主链路一致地计算窗口大小）
        """
        try:
            window_size = self._compute_window_size(emotion_intensity)
            total_count = await self.message_repo.count_by_conversation(
                conversation_id
            )
            if total_count <= settings.summary_trigger_count:
                return

            messages_to_summarize = total_count - window_size
            # 只有当新增消息超过上次摘要覆盖数时才重新生成摘要
            if messages_to_summarize <= summary_message_count:
                return

            await self._generate_incremental_summary(
                conversation_id,
                current_summary,
                summary_message_count,
                window_size,
                messages_to_summarize,
            )
        except Exception as e:
            logger.warning("后台摘要调度失败: %s", e)

    async def _generate_incremental_summary(
        self,
        conversation_id: str,
        current_summary: str | None,
        last_summary_count: int,
        window_size: int,
        new_summary_count: int,
    ) -> str | None:
        """
        增量摘要生成：只处理新增消息并与旧摘要合并，并一次性持久化。

        Args:
            conversation_id: 对话ID
            current_summary: 现有摘要（可能为None）
            last_summary_count: 上次摘要已覆盖的消息数
            window_size: 当前窗口大小
            new_summary_count: 本次摘要将覆盖的消息数（用于持久化记录）

        Returns:
            新生成的摘要文本，失败时返回旧摘要
        """
        try:
            all_messages = await self.message_repo.get_all_for_conversation(
                conversation_id
            )

            # 只取上次摘要后到当前窗口前的消息
            new_messages = all_messages[last_summary_count: -window_size]
            if not new_messages:
                return current_summary

            # 构建新增对话文本（跳过AI图片占位消息；时间感知开启时每行带日期前缀，
            # 使摘要保留时间脉络，避免 LLM 把跨天内容当作同一时段发生）
            time_tag_enabled = settings.message_time_enabled
            tz = timezone(timedelta(hours=settings.default_timezone_offset))
            # 年份规则：待摘要消息跨越年界线、或整批不属于当前年时全部带年份，
            # 否则模型无法区分去年与今年的同月同日（摘要会被长期读取，歧义代价更高）
            now_year = datetime.now(tz).year
            batch_years = {
                _ensure_aware(m.created_at).astimezone(tz).year for m in new_messages
            }
            need_year = len(batch_years) > 1 or now_year not in batch_years
            date_fmt = "%Y-%m-%d" if need_year else "%m-%d"

            def _dialog_line(m: MessageInDB) -> str:
                speaker = "身份" if m.role == "user" else "AI"
                if time_tag_enabled:
                    day = _ensure_aware(m.created_at).astimezone(tz).strftime(date_fmt)
                    return f"[{day}] {speaker}: {m.content}"
                return f"{speaker}: {m.content}"

            new_dialog = "\n".join(
                _dialog_line(m)
                for m in new_messages
                if not (m.role == "assistant" and m.content_type == "image")
            )

            if not new_dialog.strip():
                return current_summary

            # 摘要覆盖的时间范围：摘要覆盖 all_messages[:new_summary_count]，
            # 直接取首尾消息时间，无需额外读会话文档
            covered = all_messages[:new_summary_count]
            summary_start_at = covered[0].created_at if covered else None
            summary_end_at = covered[-1].created_at if covered else None

            def _fmt_date(dt: datetime | None) -> str:
                """按配置时区渲染为“8月1日”（需要时带年份，与对话行规则一致）。"""
                if dt is None:
                    return ""
                local = _ensure_aware(dt).astimezone(tz)
                prefix = f"{local.year}年" if need_year else ""
                return f"{prefix}{local.month}月{local.day}日"

            # 时间脉络硬要求：未告知覆盖范围与禁止相对表述时，增量摘要会把时间线压平
            range_text = ""
            start_cn, end_cn = _fmt_date(summary_start_at), _fmt_date(summary_end_at)
            if start_cn and end_cn:
                range_text = (
                    f"必须保留时间脉络：本次摘要覆盖 {start_cn} 至 {end_cn} 的对话。\n"
                )
            elif start_cn or end_cn:
                range_text = f"必须保留时间脉络：本次摘要覆盖 {start_cn or end_cn} 的对话。\n"
            timeline_rule = (
                f"{range_text}"
                "凡涉及具体日期的事件，摘要中必须写明绝对日期（如“8月26日面试”），"
                "禁止使用“今天/昨天/明天/最近”等相对表述（摘要会在未来被读取，相对词会失效）。"
            )

            # 根据是否有旧摘要决定prompt策略
            if current_summary:
                system_content = (
                    "你是对话摘要助手。请将已有摘要与新增对话合并为一份完整摘要。\n"
                    "要求：中文，不超过200字，保留关键信息和情感变化。\n"
                    + timeline_rule
                )
                identity_content = (
                    f"已有摘要：{current_summary}\n\n"
                    f"新增对话：\n{new_dialog}\n\n"
                    "请合并为一份完整摘要。"
                )
            else:
                system_content = (
                    "你是对话摘要助手。请将以下对话压缩为简洁摘要。\n"
                    "要求：中文，不超过200字，保留关键信息和情感变化。\n"
                    + timeline_rule
                )
                identity_content = f"对话内容：\n{new_dialog}\n\n请生成摘要。"

            provider = get_llm_provider("summary")
            response = await provider.complete(
                messages=[
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": identity_content},
                ],
                temperature=settings.summary_temperature,
                max_tokens=settings.summary_max_tokens,
            )

            # 摘要质量验证：长度需大于10字符
            if response and len(response.strip()) > 10:
                new_summary = response.strip()
                # 一次性持久化摘要、覆盖计数与覆盖时间范围（消除双写）
                await self.conversation_repo.set_summary(
                    conversation_id,
                    new_summary,
                    summary_message_count=new_summary_count,
                    summary_start_at=summary_start_at,
                    summary_end_at=summary_end_at,
                )
                logger.info(
                    "Generated incremental summary for conversation %s (covered=%d)",
                    conversation_id,
                    new_summary_count,
                )
                return new_summary

            return current_summary  # 质量不达标，保留旧摘要

        except Exception as e:
            logger.warning("增量摘要生成失败: %s", e)
            return current_summary  # 失败时保留旧摘要
