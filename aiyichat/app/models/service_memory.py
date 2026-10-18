"""服务级长期记忆的数据模型。

每个 Service（用户+Bot+角色 的绑定）拥有独立的长期记忆文档，
包含当前话题焦点和结构化的记忆事实列表。
记忆随 Service 生命周期管理，Service 删除时级联清理。
"""

from datetime import datetime, timezone
from typing import Literal, Self

import math

from pydantic import BaseModel, Field

# 记忆分层：core=核心层（安全/身份，常驻注入、永不淘汰）
#           episodic=情景层（近期事件）、semantic=语义层（一般事实）
MemoryTier = Literal["core", "episodic", "semantic"]

# 核心层类别：安全与身份类事实永不淘汰、常驻注入
CORE_CATEGORIES: frozenset[str] = frozenset({"safety", "core_identity"})


class MemoryFact(BaseModel):
    """单条记忆事实。

    时间轴采用 Graphiti/Zep 的双时间轴最小子集（仅借字段语义，不引入图数据库）：
    - 系统录入轴（transaction time）：created_at / last_referenced_at / expired_at
    - 现实成立轴（valid time）：valid_at / invalid_at
    事实被纠正时只标记失效不删除，保留可回溯的时间线。
    """

    content: str  # 记忆内容，如 "用户正在准备考研，目标是北大"
    category: str = "general"  # 分类：life_event / preference / relationship / mood_pattern / general
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    last_referenced_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    embedding: list[float] = Field(default_factory=list)  # 语义向量（可选，空则降级规则逻辑）
    # 记忆强度（艾宾浩斯强化）：被引用时 ×1.2（上限 3.0），强度越高衰减越慢
    strength: float = 1.0
    # 记忆分层：core 常驻注入且永不淘汰；episodic/semantic 参与语义检索与淘汰
    tier: MemoryTier = "semantic"
    # ── 双时间轴（均可为空；存量数据全为 None，不参与过期过滤）──
    valid_at: datetime | None = None  # 现实成立轴：事实何时发生/开始成立
    invalid_at: datetime | None = None  # 现实成立轴：事实何时不再成立
    expired_at: datetime | None = None  # 系统录入轴：系统何时标记该事实失效
    source_message_at: datetime | None = None  # provenance：事实来自哪条消息（抽取时间基点）


def _as_utc(dt: datetime) -> datetime:
    """将可能为 naive 的时间归一为 UTC aware（存量数据可能无时区信息）。"""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def is_fact_invalidated(fact: MemoryFact, now: datetime | None = None) -> bool:
    """判断事实是否已失效（注入前硬过滤的依据）。

    仅当 invalid_at / expired_at **非空且已过期** 时返回 True：
    存量数据两字段全为 None，因此不会被误杀（时间衰减仍由 score_memory_fact 负责）。
    naive 时间按 UTC 归一，避免与 aware now 相减报错。
    """
    if now is None:
        now = datetime.now(timezone.utc)
    for field_value in (fact.invalid_at, fact.expired_at):
        if field_value is None:
            continue
        if _as_utc(field_value) <= now:
            return True
    return False


class PendingEvent(BaseModel):
    """用户提及的、值得在未来某时机主动跟进的事件。"""

    content: str  # 事件描述，如 "明天有面试"
    due_at: datetime  # 预期跟进时间（UTC）
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    followed_up: bool = False  # 是否已主动跟进


class EntityRelation(BaseModel):
    """实体间关系边（如"小红的哥哥是小明"）。

    挂在 MemoryEntity.relations 数组上，支持关系变更时标记失效
    （valid=False）保留时间线，而非物理删除。
    """

    target: str  # 目标实体名，如 "小明"
    relation: str  # 关系描述，如 "哥哥"
    valid: bool = True  # 关系是否当前有效（False 表示已失效的历史关系）
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class MemoryEntity(BaseModel):
    """轻量实体关系：对话中出现的与用户提供相关的人物/事物。

    不引入图数据库，以数组形式挂在 service_memories 文档上，
    支持"小红是谁"类关系查询与命中注入。
    扩展字段支持人际关系网络：别名消歧、实体间关系、情感倾向。
    """

    name: str  # 实体名，如 "小红"
    relation_to_identity: str = ""  # 与用户关系，如 "女朋友"
    attributes: dict[str, str] = Field(default_factory=dict)  # 属性，如 {"生日": "下周三"}
    last_mentioned_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    # 实体类型：person=人物 / pet=宠物 / thing=事物
    entity_type: Literal["person", "pet", "thing"] = "person"
    # 别名/昵称列表，如 ["老王", "王哥"]，用于消歧与命中匹配
    aliases: list[str] = Field(default_factory=list)
    # 用户对该实体的情感倾向，如 "亲近" / "有矛盾"
    sentiment: str = ""
    # 实体间关系边（如"小红的哥哥是小明"）
    relations: list[EntityRelation] = Field(default_factory=list)
    # 被提及次数（用于淘汰排序：活跃度越高的实体越不易被淘汰）
    mention_count: int = 0
    # 消歧用向量（"名称+别名+与用户关系"拼接的 embedding，空则降级精确匹配）
    embedding: list[float] = Field(default_factory=list)


class CharacterMood(BaseModel):
    """角色当前心情状态（效价-唤醒二维模型）。

    挂在 service_memories 文档上，与长期记忆同生命周期；
    每轮对话后由 LLM 异步更新，读取时按时间衰减向基线回归。
    """

    valence: float = 0.0        # 愉悦度 -1.0(低落) ~ +1.0(愉悦)
    arousal: float = 0.3        # 激活度 0.0(平静) ~ 1.0(激动)
    label: str = ""             # 当前心情短语，如 "有点失落"、"心情不错"
    cause: str = ""             # 心情成因，如 "因为对方分享了好消息"
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class ServiceMemoryInDB(BaseModel):
    """数据库中的服务记忆文档。"""

    id: str = Field(alias="_id")
    service_id: str
    facts: list[MemoryFact] = Field(default_factory=list)
    current_topic: str = ""  # 当前对话焦点
    topic_history: list[str] = Field(default_factory=list)  # 话题历史（最多保存10个话题）
    pending_events: list[PendingEvent] = Field(default_factory=list)  # 待跟进事件
    entities: list[MemoryEntity] = Field(default_factory=list)  # 轻量实体关系
    mood: CharacterMood | None = None  # 角色当前心情（None 表示未启用或无状态）
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    model_config = {"populate_by_name": True}

    @classmethod
    def from_mongo(cls, doc: dict) -> Self:
        """从 MongoDB 文档构造记忆对象。"""
        if doc is None:
            raise ValueError("Document is None")
        doc["_id"] = str(doc["_id"])
        # 兼容存量数据：无 updated_at 时使用当前时间
        if "updated_at" not in doc:
            doc["updated_at"] = datetime.now(timezone.utc)
        # 兼容存量数据：无 current_topic 时设空
        doc.setdefault("current_topic", "")
        doc.setdefault("topic_history", [])
        doc.setdefault("facts", [])
        doc.setdefault("pending_events", [])
        doc.setdefault("entities", [])  # 兼容存量数据：无 entities 字段
        doc.setdefault("mood", None)  # 兼容存量数据：无 mood 字段
        # 兼容存量数据：存量实体无扩展字段，补默认值
        for ent in doc.get("entities", []):
            ent.setdefault("entity_type", "person")
            ent.setdefault("aliases", [])
            ent.setdefault("sentiment", "")
            ent.setdefault("relations", [])
            ent.setdefault("mention_count", 0)
            ent.setdefault("embedding", [])
        # 兼容存量数据：存量事实无 strength 字段，补默认值 1.0
        for fact in doc.get("facts", []):
            fact.setdefault("strength", 1.0)
            # 兼容存量数据：无 tier 字段时按类别推导层级
            if "tier" not in fact:
                fact["tier"] = (
                    "core" if fact.get("category") in CORE_CATEGORIES else "semantic"
                )
            # 兼容存量数据：双时间轴四个新字段补 None
            # （全为 None 时不参与过期硬过滤，存量记忆不会被误杀）
            for temporal_field in (
                "valid_at", "invalid_at", "expired_at", "source_message_at",
            ):
                fact.setdefault(temporal_field, None)
        # 兼容存量数据：待跟进事件无 followed_up 字段时补 False
        for event in doc.get("pending_events", []):
            event.setdefault("followed_up", False)
        return cls(**doc)


# 记忆注入排序：类别权重（重要事件/关系优先）
_CATEGORY_WEIGHTS: dict[str, float] = {
    "life_event": 1.3,
    "relationship": 1.25,
    "preference": 1.15,
    "mood_pattern": 1.1,
    "general": 1.0,
}

# 时间衰减半衰期（天）：超过该时长未被引用的记忆权重减半
_RECENCY_HALFLIFE_DAYS = 14.0

# 记忆强度上下限：被引用时强化，强度越高半衰期越长（越难忘）
_STRENGTH_MIN = 1.0
_STRENGTH_MAX = 3.0
_STRENGTH_GROWTH = 1.2  # 每次被引用强度 ×1.2


def reinforce_strength(current: float) -> float:
    """记忆被引用时的强度强化（上限 _STRENGTH_MAX）。"""
    return min(current * _STRENGTH_GROWTH, _STRENGTH_MAX)


def score_memory_fact(fact: MemoryFact, now: datetime | None = None) -> float:
    """为单条记忆事实打分，用于 Prompt 注入时的相关性排序。

    score = 时间衰减(按 last_referenced_at 指数衰减，半衰期随强度放大) × 类别权重。
    强度越高半衰期越长，被频繁引用的记忆“越记越牢”。
    仅基于现有字段计算，不改变数据结构。
    现实成立轴的过期降权另见 validity_downweight（乘在本函数结果上）。
    """
    if now is None:
        now = datetime.now(timezone.utc)
    ref = _as_utc(fact.last_referenced_at)
    age_days = max(0.0, (now - ref).total_seconds() / 86400.0)
    # 强度折减衰减：半衰期 × strength（强度越高衰减越慢）
    strength = max(_STRENGTH_MIN, fact.strength)
    halflife = _RECENCY_HALFLIFE_DAYS * strength
    recency = 0.5 ** (age_days / halflife)
    weight = _CATEGORY_WEIGHTS.get(fact.category, 1.0)
    return recency * weight


def validity_downweight(fact: MemoryFact, now: datetime | None = None) -> float:
    """现实成立轴（valid_at）的过期降权系数，取值 (0, 1]，乘在评分之上。

    一次性事件（面试/体检/约会）带 valid_at 且已过去时，按事件自身日期老化：
    即使 last_referenced_at 因被反复注入而刷新，也不会“越记越牢”。
    未声明 valid_at 的长期事实与尚未成立的未来事件恒为 1.0（不受影响）。
    """
    if fact.valid_at is None:
        return 1.0
    if now is None:
        now = datetime.now(timezone.utc)
    valid = _as_utc(fact.valid_at)
    # 未来才成立的事实不降权，到期与否交由待跟进事件的过期判定处理
    if valid >= now:
        return 1.0
    age_days = (now - valid).total_seconds() / 86400.0
    # 与 score_memory_fact 同一半衰期曲线，但基准换成现实成立轴（不受强度放大）
    return 0.5 ** (age_days / _RECENCY_HALFLIFE_DAYS)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """纯 Python 余弦相似度。任一为空或长度不等时返回 0.0。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


# 类别权重归一化系数（最大类别权重），用于将 base 分数映射到 ~0~1
_MAX_CATEGORY_WEIGHT = max(_CATEGORY_WEIGHTS.values())


def score_memory_fact_with_query(
    fact: MemoryFact,
    query_embedding: list[float],
    now: datetime | None = None,
    semantic_weight: float = 0.6,
) -> float:
    """结合语义相似度、时间衰减、类别权重的综合评分。

    score = semantic_weight × 语义相似度
          + (1 - semantic_weight) × (时效×类别 归一化)
    当 query_embedding 或 fact.embedding 为空时，退化为纯时效×类别评分。
    """
    base = score_memory_fact(fact, now)  # 时间衰减 × 类别权重
    if not query_embedding or not fact.embedding:
        return base
    sem = cosine_similarity(query_embedding, fact.embedding)  # 0~1
    base_norm = min(base / _MAX_CATEGORY_WEIGHT, 1.0)
    return semantic_weight * sem + (1.0 - semantic_weight) * base_norm
