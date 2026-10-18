"""角色心情管理模块。

为角色引入有状态的情绪系统（效价-唤醒二维模型），使角色具备"自己的心情"：
- 每轮对话后由 LLM 异步更新心情（考虑对方情绪、对话事件、角色性格）
- 读取时按时间衰减向基线回归（模拟"睡一觉气消了"）
- 渲染为 Prompt 指令注入对话，并联动 TTS 音调与图片情绪修饰（语速改由 TTS 指令文案控制）

由 character_mood_enabled 开关控制启用；关闭时所有注入点优雅降级。
"""

import logging
from datetime import datetime, timezone

from app.config import settings
from app.models.service_memory import CharacterMood

logger = logging.getLogger(__name__)

# 心情基线（时间衰减回归目标）：平静偏中性
_BASELINE_VALENCE = 0.0
_BASELINE_AROUSAL = 0.3

# 时间衰减半衰期（小时）：超过该时长心情强度减半
_DECAY_HALFLIFE_HOURS = 6.0

# 心情强度阈值：|valence| 低于该值时视为平静，不注入 Prompt
_SIGNIFICANT_VALENCE = 0.15

# 图片情绪修饰触发阈值：|valence| 达到该值才追加情绪修饰词
_IMAGE_MOOD_THRESHOLD = 0.6

# LLM 心情更新提示词
_MOOD_UPDATE_SYSTEM_PROMPT = (
    "你是角色心情分析助手。根据角色上一刻的心情、距上次更新的时长、"
    "对方本轮说了什么及其情绪、角色性格，更新角色的当前心情"
    "（只看对方的输入，角色自己刚说了什么不影响心情）。\n"
    "valence 为愉悦度（-1.0 极度低落 ~ +1.0 极度愉悦），"
    "arousal 为激活度（0.0 平静 ~ 1.0 激动）。\n"
    "label 用一句简短中文描述当前心情（如'有点失落''为对方开心''心疼对方'），"
    "cause 用一句简短中文说明心情成因（如'对方很久没来''对方分享了好消息''对方很疲惫'）。\n"
    "注意：心情会随时间自然平复；性格情绪化程度高的角色波动更大；"
    "不要过度反应，日常闲聊只引起小幅波动；"
    "对方疲惫时角色可能感到心疼；对方兴奋时角色可能感到开心；"
    "对方困惑时角色可能感到担心。\n"
    '严格输出 JSON：{"valence": 0.5, "arousal": 0.4, "label": "心情不错", "cause": "和对方聊得愉快"}'
)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class MoodManager:
    """角色心情状态的计算与更新。"""

    # ── 时间衰减 ─────────────────────────────────────────────

    @staticmethod
    def compute_decay(mood: CharacterMood, now: datetime | None = None) -> CharacterMood:
        """按时间衰减计算当前有效心情（纯函数，不写库）。

        距上次更新越久，valence/arousal 越向基线回归（指数衰减）。
        """
        if now is None:
            now = datetime.now(timezone.utc)
        updated = mood.updated_at
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        hours = max(0.0, (now - updated).total_seconds() / 3600.0)
        if hours <= 0.0:
            return mood
        factor = 0.5 ** (hours / _DECAY_HALFLIFE_HOURS)
        return CharacterMood(
            valence=_BASELINE_VALENCE + (mood.valence - _BASELINE_VALENCE) * factor,
            arousal=_BASELINE_AROUSAL + (mood.arousal - _BASELINE_AROUSAL) * factor,
            label=mood.label,
            cause=mood.cause,
            updated_at=mood.updated_at,
        )

    # ── Prompt 渲染 ──────────────────────────────────────────

    @staticmethod
    def render_mood_hint(mood: CharacterMood | None) -> str:
        """将角色心情渲染为注入 Prompt 的自然语言指令。

        心情平静（|valence| 过低）时返回空字符串，不注入。
        """
        if mood is None:
            return ""
        decayed = MoodManager.compute_decay(mood)
        if abs(decayed.valence) < _SIGNIFICANT_VALENCE:
            return ""

        # 心情描述
        if decayed.label:
            desc = decayed.label
        elif decayed.valence >= 0.5:
            desc = "心情很好"
        elif decayed.valence >= _SIGNIFICANT_VALENCE:
            desc = "心情不错"
        elif decayed.valence <= -0.5:
            desc = "心情很低落"
        else:
            desc = "心情有些低落"

        parts = [f"你现在{desc}"]
        if decayed.cause:
            parts.append(f"（因为{decayed.cause}）")

        # 语气指导（扩展）
        if decayed.valence <= -0.5:
            parts.append("，语气中可以自然流露出关心或心疼，但不要过度抱怨")
        elif decayed.valence <= -_SIGNIFICANT_VALENCE:
            parts.append("，语气中可以带一点点情绪色彩，保持自然")
        elif decayed.valence >= 0.5:
            parts.append("，语气轻快一些，可以适度表达愉悦或兴奋")
        else:
            parts.append("，语气保持自然即可")

        return "".join(parts)

    # ── 图片情绪修饰 ─────────────────────────────────────────

    @staticmethod
    def image_mood_suffix(mood: CharacterMood | None) -> str:
        """根据心情生成图片情绪修饰词（英文），追加到本人出镜的图片 prompt。

        仅心情强烈（|valence| >= 阈值）时触发，平时返回空字符串。
        """
        if mood is None:
            return ""
        decayed = MoodManager.compute_decay(mood)
        if abs(decayed.valence) < _IMAGE_MOOD_THRESHOLD:
            return ""
        if decayed.valence > 0:
            return ", warm genuine smile, bright cheerful expression"
        return ", slightly downturned lips, wistful melancholic expression"

    # ── LLM 异步更新 ─────────────────────────────────────────

    @staticmethod
    async def update_mood_llm(
        service_id: str,
        prev_mood: CharacterMood | None,
        identity_emotion: str | None,
        identity_text: str,
        personality_traits: dict[str, float],
    ) -> None:
        """每轮对话后异步更新角色心情（fire-and-forget，内部吃掉异常）。

        综合上一刻心情、时间衰减、**对方**本轮的表达与角色性格，调用轻量 LLM 更新。

        刻意不把角色自己本轮的回复作为输入：那会形成"情绪回声"闭环——
        系统把模型演出来的情绪当成真实心情写回，下一轮再注入回去，
        模型照着更强地演，情绪被自我强化（低落半衰期仅 6 小时也压不住）。
        心情应当由外部刺激（对方的话与情绪）驱动，而非由自己的输出驱动。
        """
        import json

        from app.db.repositories.service_memory_repo import ServiceMemoryRepository
        from app.llm.provider_factory import get_llm_provider

        try:
            now = datetime.now(timezone.utc)

            # 上一刻心情（含时间衰减后的有效值）
            if prev_mood is not None:
                decayed = MoodManager.compute_decay(prev_mood, now)
                hours = (now - (
                    prev_mood.updated_at
                    if prev_mood.updated_at.tzinfo
                    else prev_mood.updated_at.replace(tzinfo=timezone.utc)
                )).total_seconds() / 3600.0
                prev_desc = (
                    f"上一刻心情：valence={decayed.valence:.2f}, "
                    f"arousal={decayed.arousal:.2f}, "
                    f"心情={decayed.label or '平静'}, 成因={decayed.cause or '无'}, "
                    f"距上次更新约 {hours:.1f} 小时"
                )
            else:
                prev_desc = "上一刻心情：平静（首次建立心情状态）"

            # 性格描述（情绪化程度）
            sensitivity = personality_traits.get("sensitivity", 0.5)
            emotionality = "高" if sensitivity >= 0.7 else ("低" if sensitivity <= 0.3 else "中等")
            personality_desc = f"角色情绪化程度：{emotionality}（sensitivity={sensitivity:.1f}）"

            user_emotion_desc = identity_emotion or "中性"
            identity_msg = (
                f"{prev_desc}\n"
                f"{personality_desc}\n"
                f"本轮对方情绪：{user_emotion_desc}\n"
                f"对方说：{identity_text[:200]}"
            )

            provider = get_llm_provider("emotion")
            raw = await provider.complete(
                messages=[
                    {"role": "system", "content": _MOOD_UPDATE_SYSTEM_PROMPT},
                    {"role": "user", "content": identity_msg},
                ],
                temperature=settings.emotion_temperature,
                max_tokens=settings.emotion_max_tokens,
                response_format={"type": "json_object"},
            )
            raw = raw.strip()
            if raw.startswith("```"):
                raw = "\n".join(
                    ln for ln in raw.split("\n") if not ln.strip().startswith("```")
                )
            data = json.loads(raw)

            new_mood = CharacterMood(
                valence=_clamp(float(data.get("valence", 0.0)), -1.0, 1.0),
                arousal=_clamp(float(data.get("arousal", 0.3)), 0.0, 1.0),
                label=str(data.get("label", "")).strip()[:30],
                cause=str(data.get("cause", "")).strip()[:50],
                updated_at=now,
            )

            repo = ServiceMemoryRepository()
            await repo.update_mood(service_id, new_mood)
            logger.info(
                "角色心情已更新: service=%s, valence=%.2f, arousal=%.2f, label=%s",
                service_id, new_mood.valence, new_mood.arousal, new_mood.label,
            )
        except Exception as e:
            logger.warning("角色心情更新失败: %s", e)
