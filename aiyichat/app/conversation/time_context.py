"""时间上下文中枢：全系统使用后台配置的唯一 UTC 偏移。"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo

from app.config import settings

_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def ensure_aware(dt: datetime) -> datetime:
    """将可能为 naive 的时间转为 UTC aware（存量数据可能无时区信息）。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _fallback_tz() -> timezone:
    """按全局单一时区源构造固定偏移（clamp 到 -12~14，避免非法配置抛异常）。"""
    offset = max(-12, min(14, int(settings.default_timezone_offset)))
    return timezone(timedelta(hours=offset))


def resolve_tz() -> tuple[tzinfo, str]:
    """返回后台全局配置的 UTC 偏移。"""
    tz = _fallback_tz()
    return tz, _offset_label(tz)


def _offset_label(tz: timezone) -> str:
    """将固定偏移渲染为 "UTC+08:00" 形式。"""
    offset = tz.utcoffset(None) or timedelta(0)
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def _day_phase(hour: int) -> str:
    """按小时返回中文时段词（与主动关怀调度器 _time_hint 的分段保持一致）。"""
    if 0 <= hour < 5:
        return "凌晨"
    if 5 <= hour < 9:
        return "清晨"
    if 9 <= hour < 12:
        return "上午"
    if 12 <= hour < 14:
        return "中午"
    if 14 <= hour < 18:
        return "下午"
    if 18 <= hour < 22:
        return "晚上"
    return "深夜"


def format_gap(seconds: float) -> str:
    """将秒级间隔中文化为「3 天 2 小时」/「20 分钟」/「不到 1 分钟」。

    LLM 的日期算术能力极弱，直接给出间隔语义可免去模型自行做日期减法
    （借 SillyTavern 的 idleDuration 宏思路）。
    """
    if seconds < 60:
        return "不到 1 分钟"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, rest_m = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时" if rest_m == 0 else f"{hours} 小时 {rest_m} 分钟"
    days, rest_h = divmod(hours, 24)
    return f"{days} 天" if rest_h == 0 else f"{days} 天 {rest_h} 小时"


@dataclass(frozen=True)
class TimeContext:
    """单轮对话的时间上下文（唯一时间源 + 已解析时区 + 各粒度渲染结果）。

    由 build_context 一次性构建，本轮所有时间注入共用同一个 now_utc，
    避免跨午夜组装时各段落出现两个不同日期。
    """

    now_utc: datetime          # 唯一时间源（UTC aware）
    tz: tzinfo                 # 解析后的时区（zoneinfo 或固定偏移）
    tz_name: str               # "Asia/Shanghai" / "UTC+08:00"
    date_iso: str              # "2026-09-02"  日粒度，用于锚点
    weekday_cn: str            # "星期三"
    time_hm: str               # "14:30"      分钟粒度，用于易变位置
    day_phase: str             # "凌晨/清晨/上午/中午/下午/晚上/深夜"
    idle_seconds: float | None  # 距上一条真实对方消息（秒），无历史时为 None
    idle_phrase: str           # "3 天 2 小时" / "20 分钟" / ""

    @property
    def now_local(self) -> datetime:
        """当前时间按解析时区转换后的本地时间。"""
        return self.now_utc.astimezone(self.tz)


def build_context(
    last_identity_msg_at: datetime | None = None,
) -> TimeContext:
    """以全局时区和唯一 UTC 时间源构建单轮时间上下文。"""
    tz, tz_name = resolve_tz()
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)

    idle_seconds: float | None = None
    idle_phrase = ""
    if last_identity_msg_at is not None:
        idle_seconds = (now_utc - ensure_aware(last_identity_msg_at)).total_seconds()
        if idle_seconds > 0:
            idle_phrase = format_gap(idle_seconds)
        else:
            idle_seconds = None

    return TimeContext(
        now_utc=now_utc,
        tz=tz,
        tz_name=tz_name,
        date_iso=now_local.strftime("%Y-%m-%d"),
        weekday_cn=_WEEKDAYS[now_local.weekday()],
        time_hm=now_local.strftime("%H:%M"),
        day_phase=_day_phase(now_local.hour),
        idle_seconds=idle_seconds,
        idle_phrase=idle_phrase,
    )


def render_anchor(ctx: TimeContext) -> str:
    """渲染日期锚点段落（日粒度，置于 system prompt 最前，位置即锚点）。

    锚点必须早于任何带时间的记忆/事件注入：否则模型在第二段就读到
    「对方8月26日有面试」却还不知道今天几号，只能自行做日期减法而算错。
    同时显式定义相对时间术语，避免模型按训练先验理解「最近」。
    """
    return (
        f"【时间基准】今天是 {ctx.date_iso}（{ctx.weekday_cn}）。\n"
        "下文出现的“今天/昨天/明天/前天/最近/上次”等相对时间，"
        "一律以这个日期为基准推算，不要凭印象估计日期，也不要自行做跳月跳年的日期减法。"
    )


def render_moment(ctx: TimeContext) -> str:
    """渲染时刻与间隔语义段落（分钟粒度，紧贴 user_input 前的易变位置）。

    日粒度锚点已置于系统提示词开头；此处只放分钟级易变信息，
    避免秒级时间戳每请求都打破前缀缓存。
    """
    parts = [f"【当前时刻】{ctx.time_hm}（{ctx.day_phase}）"]
    if ctx.idle_phrase:
        parts.append(f"【对话间隔】距离对方上一条消息已经过去 {ctx.idle_phrase}。")
    return "\n".join(parts)
