"""聊天记录 → 情境片段与确定性风格画像。

这一层不调用 LLM：先把“在什么情境下怎么回复”固定为可审计数据，
再由分析器补充有证据的语义画像。片段有上限且去重，控制存储与检索成本。
"""

import hashlib
import re
from collections import Counter
from datetime import datetime, timezone

from app.chat_import.models import ChatMessage
from app.models.clone import (
    CloneEpisodeDraft,
    CloneRoleMemory,
    CloneStyleProfile,
    CloneStyleStats,
)

_MAX_CONTEXT_MESSAGES = 8
_MAX_CONTEXT_CHARS = 500
_TARGET_GROUP_GAP_SECONDS = 120
_MAX_RUNTIME_EPISODES = 200
_MAX_EPISODES_PER_SCENE = 40
_MAX_ROLE_MEMORIES = 40

_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]"
)
_TRAILING_DECOR_RE = re.compile(
    r"[\s，。！？!?、；;：:…\.~～,、\"'“”‘’（）()\[\]【】"
    r"\U0001F300-\U0001FAFF\u2600-\u27BF]+$"
)
_PARTICLES = frozenset("呀哇哈嘞喽咯哟啦嘛呢吧啊哦嗯唉诶嘿")
_NEGATIVE_WORDS = (
    "难过", "伤心", "累", "烦", "哭", "孤独", "崩溃", "生气", "委屈",
    "不舒服", "加班", "压力", "失眠", "吵架", "分手",
)
_POSITIVE_WORDS = (
    "开心", "高兴", "升职", "上岸", "成功", "太好了", "哈哈", "爽", "惊喜",
)
_CONFLICT_WORDS = ("滚", "讨厌", "不理", "吵架", "分手", "生气", "烦死")
_GREETING_WORDS = ("早安", "晚安", "在吗", "哈喽", "hello", "hi", "早上好")
_ADVICE_WORDS = ("怎么办", "建议", "应该", "要不要", "该不该")
_GENERIC_PHRASES = frozenset({
    "哈哈", "哈哈哈", "好的", "好吧", "可以", "没事", "谢谢", "知道了",
    "在吗", "嗯嗯", "哦哦", "晚安", "早安",
})


def source_hash(raw: bytes) -> str:
    """原文件摘要：不保留原文也能识别重复导入。"""
    return hashlib.sha256(raw).hexdigest()


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text)


def classify_scene(context_text: str, target_text: str) -> str:
    """按情境关键词分桶；这是检索先验，不是人格判断。"""
    text = f"{context_text}\n{target_text}".lower()
    if any(word in text for word in _CONFLICT_WORDS):
        return "conflict"
    if any(word in text for word in _NEGATIVE_WORDS):
        return "comfort"
    if any(word in text for word in _POSITIVE_WORDS):
        return "celebrate"
    if any(word in text for word in _ADVICE_WORDS):
        return "advice"
    if any(word in text for word in _GREETING_WORDS):
        return "greeting"
    if "？" in text or "?" in text or "吗" in text or "呢" in text:
        return "question"
    return "daily"


def _trim_context(messages: list[ChatMessage]) -> list[ChatMessage]:
    """保留紧邻目标回复的有限上下文，防止片段无限膨胀。"""
    selected: list[ChatMessage] = []
    total = 0
    for msg in reversed(messages[-_MAX_CONTEXT_MESSAGES:]):
        cost = len(msg.content) + len(msg.sender) + 4
        if selected and total + cost > _MAX_CONTEXT_CHARS:
            break
        selected.append(msg)
        total += cost
    selected.reverse()
    return selected


def _format_context(messages: list[ChatMessage]) -> str:
    return "\n".join(f"{m.sender}: {m.content}" for m in messages)


def _episode_quality(context: str, target_text: str) -> float:
    target_len = len(_normalize(target_text.replace("|||", "")))
    length_score = min(target_len / 40.0, 1.0)
    context_score = min(len(context) / 80.0, 1.0)
    return round(0.7 * length_score + 0.3 * context_score, 4)


def build_episodes(
    messages: list[ChatMessage], target_name: str
) -> list[CloneEpisodeDraft]:
    """按“对方发言 → 目标连续回复”还原情境片段。

    连续目标气泡合并为一轮，保留 ``|||`` 分隔；纯媒体或无上下文回复
    不进入运行期示例库，但仍会进入整体风格统计。
    """
    drafts: list[CloneEpisodeDraft] = []
    pending_context: list[ChatMessage] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if not msg.is_target:
            pending_context.append(msg)
            i += 1
            continue

        turn = [msg]
        j = i + 1
        while j < len(messages) and messages[j].is_target:
            gap = (messages[j].timestamp - turn[-1].timestamp).total_seconds()
            if gap > _TARGET_GROUP_GAP_SECONDS:
                break
            turn.append(messages[j])
            j += 1

        context_msgs = _trim_context(pending_context)
        context_text = _format_context(context_msgs)
        target_text = "|||".join(m.content for m in turn)
        has_text = any(
            m.message_type == "text" and bool(_normalize(m.content))
            for m in turn
        )
        if context_text and has_text:
            drafts.append(
                CloneEpisodeDraft(
                    source_indexes=[m.source_index for m in context_msgs + turn],
                    started_at=context_msgs[0].timestamp,
                    ended_at=turn[-1].timestamp,
                    context_text=context_text,
                    target_text=target_text,
                    scene=classify_scene(context_text, target_text),
                    quality=_episode_quality(context_text, target_text),
                )
            )
        pending_context = []
        i = j
    return drafts


def select_runtime_episodes(
    drafts: list[CloneEpisodeDraft],
    *,
    limit: int = _MAX_RUNTIME_EPISODES,
    per_scene: int = _MAX_EPISODES_PER_SCENE,
) -> list[CloneEpisodeDraft]:
    """每场景保留高质量且不重复的片段；避免热门场景挤满整个示例库。"""
    by_scene: dict[str, list[CloneEpisodeDraft]] = {}
    for episode in drafts:
        by_scene.setdefault(episode.scene, []).append(episode)

    selected: list[CloneEpisodeDraft] = []
    seen: set[str] = set()
    for scene in sorted(by_scene):
        picked = 0
        for episode in sorted(by_scene[scene], key=lambda e: e.quality, reverse=True):
            key = _normalize(episode.target_text)
            if key in seen:
                continue
            seen.add(key)
            selected.append(episode)
            picked += 1
            if picked >= per_scene or len(selected) >= limit:
                break
        if len(selected) >= limit:
            break
    selected.sort(key=lambda e: (e.started_at, e.ended_at))
    return selected


def _percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * pct))]


def _ending_particle(text: str) -> str:
    core = _TRAILING_DECOR_RE.sub("", _normalize(text))
    return core[-1] if core and core[-1] in _PARTICLES else ""


def _stats(
    texts: list[str],
    *,
    total_turns: int = 0,
    multi_bubble_turns: int = 0,
) -> CloneStyleStats:
    cleaned = [_normalize(t) for t in texts if _normalize(t)]
    if not cleaned:
        return CloneStyleStats()
    lengths = [len(t) for t in cleaned]
    return CloneStyleStats(
        count=len(cleaned),
        median_chars=int(_percentile(lengths, 0.5)),
        p90_chars=_percentile(lengths, 0.9),
        short_rate=round(sum(n <= 4 for n in lengths) / len(cleaned), 4),
        multi_bubble_rate=(
            round(multi_bubble_turns / total_turns, 4) if total_turns else 0.0
        ),
        emoji_rate=round(
            sum(bool(_EMOJI_RE.search(t)) for t in cleaned) / len(cleaned), 4
        ),
        question_rate=round(
            sum(("？" in t or "?" in t or t.endswith(("吗", "呢")))
                for t in cleaned) / len(cleaned),
            4,
        ),
        ellipsis_rate=round(
            sum(("…" in t or "..." in t) for t in cleaned) / len(cleaned), 4
        ),
    )


def compute_style_profile(
    messages: list[ChatMessage], episodes: list[CloneEpisodeDraft]
) -> CloneStyleProfile:
    """从全部目标消息计算风格；不因消息短、只有媒体而丢掉证据。"""
    target_msgs = [m for m in messages if m.is_target]
    text_msgs = [m for m in target_msgs if m.message_type == "text"]
    target_texts = [m.content for m in text_msgs]
    all_episode_turns = len(episodes)
    multi_turns = sum(
        1 for e in episodes if len([x for x in e.target_text.split("|||") if x])
        > 1
    )

    by_scene: dict[str, CloneStyleStats] = {}
    for scene in sorted({e.scene for e in episodes}):
        scene_eps = [e for e in episodes if e.scene == scene]
        scene_texts = [
            part
            for episode in scene_eps
            for part in episode.target_text.split("|||")
            if part.strip() and part.strip() not in {
                "[图片]", "[语音]", "[视频]", "[表情]", "[文件]",
            }
        ]
        by_scene[scene] = _stats(
            scene_texts,
            total_turns=len(scene_eps),
            multi_bubble_turns=sum(
                1 for e in scene_eps
                if len([x for x in e.target_text.split("|||") if x]) > 1
            ),
        )

    emoji_counter: Counter[str] = Counter()
    particle_counter: Counter[str] = Counter()
    phrase_counter: Counter[str] = Counter()
    for text in target_texts:
        emoji_counter.update(_EMOJI_RE.findall(text))
        particle = _ending_particle(text)
        if particle:
            particle_counter[particle] += 1
        normalized = _normalize(text)
        if 2 <= len(normalized) <= 20 and normalized not in _GENERIC_PHRASES:
            phrase_counter[normalized] += 1

    common_phrases = [
        phrase for phrase, count in phrase_counter.most_common(20) if count >= 2
    ][:10]
    return CloneStyleProfile(
        sample_count=len(target_msgs),
        text_count=len(text_msgs),
        media_count=sum(1 for m in target_msgs if m.message_type == "media"),
        overall=_stats(
            target_texts,
            total_turns=all_episode_turns,
            multi_bubble_turns=multi_turns,
        ),
        by_scene=by_scene,
        common_emojis=[emoji for emoji, _ in emoji_counter.most_common(10)],
        common_particles=[
            particle for particle, _ in particle_counter.most_common(8)
        ],
        common_phrases=common_phrases,
    )


def merge_role_memories(
    chunk_results: list[dict], max_items: int = _MAX_ROLE_MEMORIES
) -> list[CloneRoleMemory]:
    """合并分块产出的角色自述事实，按内容去重并保留分块证据。"""
    merged: list[CloneRoleMemory] = []
    seen: set[str] = set()
    for chunk_index, result in enumerate(chunk_results):
        facts = result.get("role_facts", [])
        if not isinstance(facts, list):
            continue
        for item in facts:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content", "")).strip()
            key = _normalize(content)
            if len(key) < 4 or key in seen:
                continue
            try:
                confidence = float(item.get("confidence", 0.5))
            except (TypeError, ValueError):
                confidence = 0.5
            seen.add(key)
            merged.append(
                CloneRoleMemory(
                    content=content,
                    kind=str(item.get("kind", "general"))[:30] or "general",
                    confidence=round(max(0.0, min(1.0, confidence)), 2),
                    source_chunk_indexes=[chunk_index],
                )
            )
            if len(merged) >= max_items:
                return merged
    return merged


def collect_role_fact_queries(memories: list[CloneRoleMemory]) -> list[str]:
    """供检索用：只返回启用中的角色自述。"""
    return [m.content for m in memories if not m.disabled and m.content]


def dedupe_runtime_key(episode: CloneEpisodeDraft) -> str:
    """运行期去重键。"""
    return _normalize(episode.context_text[-120:] + "|" + episode.target_text)


def render_style_hint(profile: CloneStyleProfile) -> str:
    """把确定性统计压成稳定前缀用的一段中文，避免数字型提示喧宾夺主。"""
    stats = profile.overall
    if not stats.count:
        return ""
    scene_names = {
        "daily": "日常",
        "comfort": "安慰",
        "celebrate": "报喜",
        "conflict": "冲突",
        "advice": "商量",
        "greeting": "问候",
        "question": "问答",
    }
    scenes = "、".join(
        scene_names.get(scene, scene)
        for scene, value in profile.by_scene.items()
        if value.count
    )
    notes = [
        f"参考 {stats.count} 条真实回复",
        f"中位约{stats.median_chars}字、p90约{stats.p90_chars}字",
        f"短回复占比{stats.short_rate:.0%}",
        f"emoji消息占比{stats.emoji_rate:.0%}",
    ]
    if profile.common_particles:
        notes.append(f"句末常用语气词：{'、'.join(profile.common_particles[:5])}")
    if profile.common_emojis:
        notes.append(f"常用表情：{' '.join(profile.common_emojis[:6])}")
    if scenes:
        notes.append(f"可用情境样本：{scenes}")
    return (
        "；".join(notes)
        + "。这是统计倾向，不是每条回复都必须满足的硬指标。"
    )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
