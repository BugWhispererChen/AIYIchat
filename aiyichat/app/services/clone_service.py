"""克隆语料检索与运行期上下文渲染。

只读取已激活、属于当前角色账号的版本；任何检索失败都返回空上下文，
对话继续使用原有 Profile，不阻塞主链路。
"""

import asyncio
import logging
from dataclasses import dataclass, field

from app.chat_import.clone_corpus import classify_scene
from app.config import settings
from app.db.repositories.clone_repo import (
    CloneDatasetRepository,
    CloneEpisodeRepository,
)
from app.models.clone import (
    CloneEpisodeDraft,
    CloneEpisodeInDB,
    CloneRoleMemory,
    CloneStyleProfile,
)
from app.models.profile import PromptProfileInDB
from app.models.service_memory import cosine_similarity
from app.services.embedding_service import (
    embed_within_budget,
    get_embedding_service,
)

logger = logging.getLogger(__name__)

_MAX_CANDIDATES = 400
_ROLE_MEMORY_MIN_SCORE = 0.2


@dataclass
class CloneContext:
    """本轮实际注入的克隆证据，供 Prompt 与注入快照共用。"""

    examples: list[CloneEpisodeInDB] = field(default_factory=list)
    role_memories: list[CloneRoleMemory] = field(default_factory=list)
    scores: list[float] = field(default_factory=list)
    dataset_version: int = 0

    @property
    def empty(self) -> bool:
        return not self.examples and not self.role_memories


def _ngrams(text: str, sizes: tuple[int, ...] = (2, 3)) -> set[str]:
    clean = "".join(ch for ch in text if ch.isalnum())
    grams: set[str] = set()
    for size in sizes:
        grams.update(clean[i:i + size] for i in range(max(0, len(clean) - size + 1)))
    return grams


def _lexical_score(query: str, candidate: str) -> float:
    """字符 n-gram Dice：不依赖分词库，适配中文短消息。"""
    q = _ngrams(query)
    c = _ngrams(candidate)
    if not q or not c:
        return 0.0
    return 2.0 * len(q & c) / (len(q) + len(c))


def _combined_query(query: str, history: list | None) -> tuple[str, str]:
    """返回（完整情境文本，用于字符/场景；最近上下文）。"""
    context_parts: list[str] = []
    for msg in (history or [])[-4:]:
        content = getattr(msg, "content", "") or ""
        if content:
            context_parts.append(content[:160])
    context = "\n".join(context_parts)
    return f"{context}\n{query}".strip(), context


def _scene_match(query_scene: str, candidate_scene: str) -> float:
    if query_scene == candidate_scene:
        return 1.0
    related = {
        frozenset({"daily", "question"}),
        frozenset({"comfort", "conflict"}),
        frozenset({"celebrate", "daily"}),
    }
    return 0.5 if frozenset({query_scene, candidate_scene}) in related else 0.0


def _score_episode(
    episode: CloneEpisodeInDB,
    *,
    query_text: str,
    query_scene: str,
    query_embedding: list[float],
) -> float:
    query_for_lexical = _combined_query(query_text, None)[0]
    lexical = _lexical_score(query_for_lexical, episode.context_text)
    scene = _scene_match(query_scene, episode.scene)
    dense = (
        cosine_similarity(query_embedding, episode.context_embedding)
        if query_embedding and episode.context_embedding else 0.0
    )
    if dense > 0:
        return 0.55 * dense + 0.25 * lexical + 0.20 * scene
    return 0.60 * lexical + 0.40 * scene


def _mmr_select(
    scored: list[tuple[CloneEpisodeInDB, float]], top_k: int
) -> list[tuple[CloneEpisodeInDB, float]]:
    """在相关性与多样性之间折中，避免两条近乎相同的口癖占满预算。"""
    selected: list[tuple[CloneEpisodeInDB, float]] = []
    remaining = list(scored)
    while remaining and len(selected) < top_k:
        best_idx = 0
        best_value = -1.0
        for idx, (episode, score) in enumerate(remaining):
            redundancy = max(
                (
                    _lexical_score(episode.target_text, picked.target_text)
                    for picked, _ in selected
                ),
                default=0.0,
            )
            value = 0.75 * score - 0.25 * redundancy
            if value > best_value:
                best_idx, best_value = idx, value
        selected.append(remaining.pop(best_idx))
    return selected


def rank_episode_drafts_lexical(
    drafts: list[CloneEpisodeDraft],
    *,
    text: str,
    history: list | None = None,
    top_k: int = 2,
    min_score: float = 0.15,
) -> list[tuple[CloneEpisodeDraft, float]]:
    """供离线评测复用的无数据库检索（相同字符/场景打分与 MMR）。"""
    full_query, context = _combined_query(text, history)
    query_scene = classify_scene(context, text)
    scored = [
        (draft, 0.6 * _lexical_score(full_query, draft.context_text)
         + 0.4 * _scene_match(query_scene, draft.scene))
        for draft in drafts
    ]
    scored = [item for item in scored if item[1] >= min_score]
    scored.sort(key=lambda item: item[1], reverse=True)
    selected: list[tuple[CloneEpisodeDraft, float]] = []
    remaining = list(scored)
    while remaining and len(selected) < top_k:
        best_idx = 0
        best_value = -1.0
        for idx, (draft, score) in enumerate(remaining):
            redundancy = max(
                (
                    _lexical_score(draft.target_text, picked.target_text)
                    for picked, _ in selected
                ),
                default=0.0,
            )
            value = 0.75 * score - 0.25 * redundancy
            if value > best_value:
                best_idx, best_value = idx, value
        selected.append(remaining.pop(best_idx))
    return selected


async def retrieve_clone_context(
    profile: PromptProfileInDB,
    *,
    text: str,
    history: list | None = None,
    query_embedding: list[float] | None = None,
) -> CloneContext:
    """按当前情境取两组对话对和最多两条角色自述。"""
    if (
        not settings.clone_retrieval_enabled
        or not getattr(profile, "clone_enabled", False)
        or getattr(profile, "clone_status", "") != "active"
        or not getattr(profile, "clone_dataset_id", "")
        or not getattr(profile, "account_id", None)
    ):
        return CloneContext()

    try:
        dataset = await CloneDatasetRepository().find_by_id(profile.clone_dataset_id)
        if (
            dataset is None
            or dataset.status != "active"
            or dataset.account_id != profile.account_id
            or dataset.profile_id != profile.id
            or dataset.version != profile.clone_version
        ):
            logger.warning(
                "克隆语料版本校验失败: profile=%s dataset=%s",
                profile.id, profile.clone_dataset_id,
            )
            return CloneContext()

        episodes = await CloneEpisodeRepository().list_runtime(
            profile_id=profile.id,
            clone_version=profile.clone_version,
            account_id=profile.account_id,
            limit=_MAX_CANDIDATES,
        )
        full_query, context = _combined_query(text, history)
        query_scene = classify_scene(context, text)

        vector = query_embedding or []
        if not vector and episodes:
            vector = await embed_within_budget(
                get_embedding_service(), text, what="克隆片段语义召回"
            )

        scored = [
            (episode, _score_episode(
                episode,
                query_text=full_query,
                query_scene=query_scene,
                query_embedding=vector,
            ))
            for episode in episodes
        ]
        scored.sort(key=lambda item: item[1], reverse=True)
        relevant = [
            item for item in scored
            if item[1] >= settings.clone_retrieval_min_score
        ]
        picked = _mmr_select(relevant, max(1, settings.clone_retrieval_topk))

        memory_scores = [
            (memory, _lexical_score(full_query, memory.content))
            for memory in dataset.role_memories
            if not memory.disabled
        ]
        memory_scores.sort(key=lambda item: item[1], reverse=True)
        memories = [
            memory for memory, score in memory_scores[:2]
            if score >= _ROLE_MEMORY_MIN_SCORE
        ]

        return CloneContext(
            examples=[episode for episode, _ in picked],
            role_memories=memories,
            scores=[round(score, 4) for _, score in picked],
            dataset_version=dataset.version,
        )
    except Exception as e:
        logger.warning("克隆语料检索失败，降级为 Profile-only: %s", e)
        return CloneContext()


def render_clone_context(
    context: CloneContext, *, max_chars: int | None = None
) -> str:
    """渲染为动态尾段；明确区分“风格证据”和“当前事实”。"""
    if context.empty:
        return ""
    budget = max_chars or settings.clone_retrieval_max_chars
    sections: list[str] = []
    if context.role_memories:
        lines = "\n".join(f"- {m.content}" for m in context.role_memories[:2])
        sections.append(
            "【你的相关经历】\n"
            f"{lines}\n"
            "这些是你本人过去的经历，不是对方刚说的事；只在自然相关时提起。"
        )
    if context.examples:
        blocks: list[str] = []
        scene_names = {
            "daily": "日常", "comfort": "安慰", "celebrate": "报喜",
            "conflict": "冲突", "advice": "商量", "greeting": "问候",
            "question": "问答",
        }
        for idx, episode in enumerate(context.examples, start=1):
            blocks.append(
                f"参考{idx}（{scene_names.get(episode.scene, episode.scene)}）\n"
                f"对方：{episode.context_text}\n"
                f"你当时：{episode.target_text.replace('|||', ' / ')}"
            )
        sections.append(
            "【相似情境下的表达参考】\n"
            + "\n\n".join(blocks)
            + "\n以上是风格与当时的表达证据，不是当前发生的事实。"
            "只借鉴句式、语气、长度和承接方式；不要照抄、不要复述其中事实，"
            "也不要把示例中的关系当作你与当前对方的关系。"
        )
    text = "\n\n".join(sections)
    if len(text) > budget:
        text = text[:budget].rstrip() + "\n（证据过长已截断）"
    return text


def render_style_hint(style: CloneStyleProfile) -> str:
    """兼容从数据集读取风格画像时的呈现。"""
    from app.chat_import.clone_corpus import render_style_hint as _render

    return _render(style)


async def backfill_clone_embeddings(dataset_id: str) -> None:
    """激活后异步补齐片段向量；失败或未配置 embedding 时保留字符检索。

    这里在离线后台任务里按 10 条分批、每批 30 秒，不能复用对话链路的 3 秒检索
    预算：一次导入可能有 200 个片段，共用短预算会让回填几乎必然整体超时。
    """
    try:
        repo = CloneEpisodeRepository()
        episodes = await repo.list_by_dataset(dataset_id)
        pending = [e for e in episodes if not e.context_embedding]
        if not pending:
            return
        service = get_embedding_service()
        if not service.enabled:
            return
        filled = 0
        for start in range(0, len(pending), 10):
            batch = pending[start:start + 10]
            try:
                vectors = await asyncio.wait_for(
                    service.embed_batch([e.context_text for e in batch]),
                    timeout=30.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "克隆语料向量回填单批超时: dataset=%s batch=%d",
                    dataset_id, start // 10,
                )
                continue
            for episode, vector in zip(batch, vectors):
                if vector:
                    await repo.update_embedding(episode.id, vector)
                    filled += 1
        logger.info(
            "克隆语料向量回填完成: dataset=%s, filled=%d/%d",
            dataset_id, filled, len(pending),
        )
    except Exception as e:
        logger.warning("克隆语料向量回填失败: dataset=%s error=%s", dataset_id, e)
