"""角色作息（C1）的纯状态机：把"角色本地此刻几点"映射成"此刻你在做什么"。

设计约束（都是踩过的坑，勿回退）：
- **只注入处境、不改变投递**：作息绝不能让角色"睡着就不回消息"或拖延回复，
  这是用户两次明确否决过的方向，护栏文案直接写进注入内容里；
- **输出只进动态尾段**：任何按分钟/小时变化的文本都不许进 system 前缀，
  否则每轮都击穿 prompt 前缀缓存，首字延迟从几秒恶化到几十秒；
- **零模型调用**：纯本地计算，不额外花一次 LLM。
"""

from datetime import datetime

from app.models.profile import ProfileLifeRoutine, ProfileLifeSegment

# 注入文案的固定护栏：作息只影响"说什么怎么说"，不影响"发不发"
_ROUTINE_GUARD = (
    "只据此决定你在做什么、回得快慢；不向对方报备行程，"
    "也绝不因此不回消息或拖延回复。"
)


def _matches_day(segment: ProfileLifeSegment, weekday: int) -> bool:
    """`days` 为空表示每天；否则只在列出的工作日命中。"""
    return not segment.days or weekday in segment.days


def _matches_hour(segment: ProfileLifeSegment, hour: int) -> bool:
    """时段命中：start 含、end 不含；start > end 视为跨午夜。"""
    start, end = segment.start_hour, segment.end_hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    # 跨午夜（如 22~6）：23 点与 3 点都算命中
    return hour >= start or hour < end


def current_segment(
    routine: ProfileLifeRoutine, now_local: datetime
) -> ProfileLifeSegment | None:
    """返回此刻命中的作息段；未配置、未命中或时段非法时返回 None。

    按列表顺序取第一条命中，运营可用"先具体后笼统"的顺序表达例外。
    """
    if not routine or not routine.segments:
        return None
    for segment in routine.segments:
        if _matches_day(segment, now_local.weekday()) and _matches_hour(
            segment, now_local.hour
        ):
            return segment
    return None


def render_routine_hint(
    routine: ProfileLifeRoutine, now_local: datetime
) -> str:
    """渲染【你的作息】一行上下文提示；没有可注入内容时返回空串（调用方据此跳过）。"""
    segment = current_segment(routine, now_local)
    if segment is None or not segment.activity.strip():
        return ""
    moment = f"{now_local.strftime('%H:%M')}，{_weekday_cn(now_local.weekday())}"
    detail = (
        f"「{segment.activity.strip()}」——{segment.phrase.strip()}"
        if segment.phrase.strip()
        else f"「{segment.activity.strip()}」"
    )
    return f"【你的作息】此刻（{moment}，你所在时区）是你在{detail}。{_ROUTINE_GUARD}"


def _weekday_cn(weekday: int) -> str:
    """周一~周日的中文写法（与 _WEEKDAYS 口径一致，0=周一）。"""
    names = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
    return names[weekday % 7]
