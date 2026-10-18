"""
主动消息发送策略定义。

定义触发时机、阶段划分、内容生成规则。
QQ平台策略：约2天内按4个阶段发送互动召回消息（4h/14h/28h/44h）。
微信平台策略：基于缓存 context_token 发送主动消息。
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class ProactiveStage:
    """主动消息阶段定义。"""

    stage: int  # 阶段编号（1-4）
    delay_hours: float  # 对方最后活跃后多少小时触发
    content_hint: str  # 内容生成提示词
    emotion_hints: dict[str, str] | None = None  # 按情绪调整的内容提示


# QQ平台：匹配互动召回消息周期的4阶段策略
# QQ召回消息周期：当天/1-3天/3-7天/7-30天，每个周期可发1条
# 阶段延迟设计：对齐召回周期，确保每个阶段在对应周期内触发
# 阶段1: 4h   —— 当天召回（轻松问候）
# 阶段2: 24h  —— 1-3天召回（分享话题）
# 阶段3: 72h  —— 3-7天召回（温柔关心）
# 阶段4: 168h —— 7-30天召回（暖心鼓励）
QQ_STAGES: list[ProactiveStage] = [
    ProactiveStage(
        stage=1,
        delay_hours=4.0,
        content_hint="轻松问候，关心对方现在的状态，语气自然不刻意",
        emotion_hints={
            "sad": "温柔地表达关心和理解，轻声问候对方现在感觉如何",
            "anxious": "轻松自然地问候，聊些让人放松的小事",
            "lonely": "亲切地表达想念，让对方知道有人在",
            "angry": "温和地问候，不提及让对方不快的事",
            "happy": "开心地打招呼，延续快乐氛围",
        },
    ),
    ProactiveStage(
        stage=2,
        delay_hours=24.0,
        content_hint="分享一个有趣的想法、话题或小故事，引发对方兴趣",
        emotion_hints={
            "sad": "分享一个温暖治愈的小故事或有趣发现，带来一点光亮",
            "anxious": "分享一个轻松有趣的话题，帮助转移注意力",
            "lonely": "分享日常趣事，营造陪伴感",
            "angry": "分享一个有趣的见闻，缓和心情",
            "happy": "分享一个有趣的想法或话题，一起开心",
        },
    ),
    ProactiveStage(
        stage=3,
        delay_hours=72.0,
        content_hint="温柔关心对方的生活近况，表达想念之情",
        emotion_hints={
            "sad": "温柔地关心对方最近有没有好一些，表达持续的关注",
            "anxious": "轻轻地问对方最近忙不忙，不要施加压力",
            "lonely": "表达真挚的想念，关心对方的日常",
            "angry": "温和地问候近况，表达理解和支持",
            "happy": "开心地问对方最近过得如何，分享近况",
        },
    ),
    ProactiveStage(
        stage=4,
        delay_hours=168.0,
        content_hint="暖心的鼓励或祝福话语，给对方留下温暖印象",
        emotion_hints={
            "sad": "给予温暖的鼓励，表达相信对方会好起来的信念",
            "anxious": "给予安心的祝福，传达一切都会好的温暖",
            "lonely": "传达温暖的陪伴之意，让对方感到被惦记",
            "angry": "平和地送上祝福，表达关怀",
            "happy": "送上美好祝福，为对方的快乐加油",
        },
    ),
]

# 微信平台：复用相同的阶段策略（包含 emotion_hints）
WECHAT_STAGES: list[ProactiveStage] = QQ_STAGES


def get_pending_stage(
    last_active_at: datetime,
    sent_stages: list[int],
    platform: str = "qq",
) -> ProactiveStage | None:
    """
    根据对方最后活跃时间和已发送阶段，判断当前应发送的阶段。

    Args:
        last_active_at: 对方最后活跃时间（UTC）
        sent_stages: 已成功发送的阶段编号列表
        platform: 平台标识

    Returns:
        需要发送的阶段对象，或 None（无需发送）
    """
    now = datetime.now(timezone.utc)
    # MongoDB 读出的 datetime 可能为 naive（无时区），按 UTC 归一后再相减
    if last_active_at.tzinfo is None:
        last_active_at = last_active_at.replace(tzinfo=timezone.utc)
    elapsed_hours = (now - last_active_at).total_seconds() / 3600

    stages = QQ_STAGES if platform == "qq" else WECHAT_STAGES

    for stage in stages:
        # 跳过已发送的阶段
        if stage.stage in sent_stages:
            continue
        # 超过30天(720h)不再发送
        if elapsed_hours > 720.0:
            return None
        # 达到延迟时间 → 应该发送
        if elapsed_hours >= stage.delay_hours:
            return stage

    return None
