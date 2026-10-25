from dataclasses import dataclass, field

from app.db.repositories.bot_config_repo import BotConfigRepository
from app.db.repositories.conversation_repo import ConversationRepository
from app.db.repositories.message_repo import MessageRepository
from app.db.repositories.profile_repo import ProfileRepository
from app.db.repositories.identity_repo import IdentityRepository


@dataclass
class DashboardStats:
    profile_count: int = 0
    active_profile_count: int = 0
    identity_count: int = 0
    conversation_count: int = 0
    message_total: int = 0
    message_today: int = 0
    bot_count: int = 0


async def get_dashboard_stats() -> DashboardStats:
    profile_repo = ProfileRepository()
    identity_repo = IdentityRepository()
    conv_repo = ConversationRepository()
    msg_repo = MessageRepository()
    bot_repo = BotConfigRepository()

    all_profiles = await profile_repo.list_all()
    active_profiles = [p for p in all_profiles if p.is_active]

    stats = DashboardStats(
        profile_count=len(all_profiles),
        active_profile_count=len(active_profiles),
        identity_count=await identity_repo.count(),
        conversation_count=await conv_repo.count(),
        message_total=await msg_repo.count_total(),
        message_today=await msg_repo.count_today(),
        bot_count=await bot_repo.count(),
    )
    return stats


# 情绪标签→中文展示名（覆盖 emotion_detector 支持的全部标签）
_EMOTION_LABELS: dict[str, str] = {
    "sad": "悲伤",
    "angry": "愤怒",
    "anxious": "焦虑",
    "happy": "开心",
    "lonely": "孤独",
    "tired": "疲惫",
    "excited": "兴奋",
    "confused": "困惑",
    "nostalgic": "怀念",
    "grateful": "感激",
    "shy": "害羞",
}


@dataclass
class DashboardCharts:
    """仪表盘趋势图表数据（供 Chart.js 渲染）。"""

    msg_daily_labels: list = field(default_factory=list)   # 日期标签
    msg_daily_values: list = field(default_factory=list)   # 每日消息量
    latency_labels: list = field(default_factory=list)     # 日期标签
    latency_values: list = field(default_factory=list)     # 每日平均延迟(ms)
    emotion_labels: list = field(default_factory=list)     # 情绪中文名
    emotion_values: list = field(default_factory=list)     # 情绪计数


async def get_dashboard_charts(days: int = 14) -> DashboardCharts:
    """聚合仪表盘趋势图表所需数据。

    消息量/延迟按 days 天，情绪分布固定统计近 7 天。
    """
    msg_repo = MessageRepository()

    daily = await msg_repo.count_daily(days)
    latency = await msg_repo.avg_latency_daily(days)
    emotions = await msg_repo.emotion_distribution(7)

    return DashboardCharts(
        msg_daily_labels=[d["date"] for d in daily],
        msg_daily_values=[d["count"] for d in daily],
        latency_labels=[d["date"] for d in latency],
        latency_values=[d["avg_ms"] for d in latency],
        emotion_labels=[_EMOTION_LABELS.get(e["emotion"], "其他") for e in emotions],
        emotion_values=[e["count"] for e in emotions],
    )
