"""反思层：从既有记忆事实中归纳跨时间的高阶判断（洞见）。

为什么不是"再抽象一层"
----------------------
业界标准做法（Generative Agents）假设底层是原子观察，反思负责抽象。本项目不成立：
测试服取证显示抽取器产出的本身就是半抽象语句（"对方独居且感到孤独"、"用户对努力的
意义产生怀疑"）。在它之上二次抽象，得到的只是把同一批话换个说法再写一遍，而风险
（错误归纳被后续反思反复加固）是实打实的。

所以这里把反思定义为**聚合器**而非抽象器：只归纳"单轮窗口看不见"的东西——
跨周持续的状态、关系走向、以及多条事实合起来才成立的模式。抽取器每 6 条消息看一次
局部窗口，它结构上无法产出"这三周对方一直在加班"这类判断，这才是真实缺口。

与业界的两处刻意差异
--------------------
1. **触发不用 LLM 逐条打 importance**：本项目的 `strength` 实测已有 58% 顶到上限 3.0，
   作为判别信号退化；而逐条打分要按事实条数加模型调用。改用量纲明确的双条件：
   新增事实数达门槛 **且** 距上次反思达最小间隔。
2. **回注走常驻稳定段而不是参与检索**：让洞见与事实抢 top-k 名额，在本项目等于
   基本不出场（默认部署语义检索未开、且 3 个服务里只有 1 个真有向量）。
   代价是错误洞见会持续影响人格，因此 grounding 与后台撤销入口是必需项而非可选。
"""

import logging
from datetime import datetime, timezone

from app.config import settings
from app.conversation.memory_hygiene import new_fact_id
from app.llm.provider_factory import get_llm_provider
from app.models.service_memory import MemoryFact, MemoryInsight

logger = logging.getLogger(__name__)

# 洞见归纳提示词：要求"必须点名依据哪几条事实"，且禁止复述单条事实本身
_REFLECT_SYSTEM_PROMPT = (
    "你在为一段长期陪伴对话做回顾整理。下面给出的是关于对方的既有记忆条目"
    "（每条前有编号）。请只归纳**把多条记忆合在一起才成立**的高阶判断：\n"
    "- 跨越一段时间持续出现的状态或趋势（trend）\n"
    "- 对方与其生活中某人/某物的关系走向（relationship）\n"
    "- 对方当前正处在的阶段性处境（state）\n\n"
    "硬性要求：\n"
    "1. 每条判断必须同时依据至少 {min_evidence} 条不同的既有记忆，"
    "evidence 字段原样填写这些记忆的编号；\n"
    "2. 只复述单条记忆能得出的内容不算判断，不要输出；\n"
    "3. 最多输出 {max_insights} 条，每条不超过 22 个字，用「对方」称述用户；\n"
    "4. 不得引入既有记忆中没有的事实（不许推测、不许补全、不许编造共同经历）。\n\n"
    "严格输出 JSON，不要其他内容：\n"
    '{{"insights": [{{"content": "判断文本", "kind": "trend", '
    '"evidence": [1, 4]}}]}}\n'
    "没有足够依据时返回空数组：{{\"insights\": []}}"
)

_VALID_KINDS = ("trend", "relationship", "state")


def should_reflect(
    fact_count: int,
    last_reflection_at: datetime | None,
    now: datetime | None = None,
) -> bool:
    """双条件触发：新增事实足够多 **且** 距上次反思足够久。

    以 `fact_count` 而非"累计重要性"作门槛：`strength` 已普遍饱和，判不出差异。
    存量口径下 fact_count 是当前事实总数——首次反思因此需要档案长到门槛才触发，
    这正是我们想要的（材料不足时归纳出来的只能是复述）。
    """
    if not settings.memory_reflection_enabled:
        return False
    if now is None:
        now = datetime.now(timezone.utc)
    if fact_count < settings.reflection_min_facts:
        return False
    if last_reflection_at is None:
        return True
    last = last_reflection_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    hours_since = (now.astimezone(timezone.utc) - last).total_seconds() / 3600.0
    return hours_since >= settings.reflection_min_interval_hours


def build_evidence_block(facts: list[MemoryFact]) -> tuple[str, dict[int, str]]:
    """把事实编号成 1..n 的清单，并返回 编号 → fid 映射（grounding 的服务端依据）。

    只送未失效的事实进反思：让模型基于一堆已经过期或被纠正的记忆归纳，
    得到的洞见会把这些记忆"复活"。

    清单按 `reflection_max_evidence` 截断（保留最后引用时间最新的）：
    反思不需要读完全部记忆，而推理型模型的耗时与 token 消耗随清单长度线性上涨，
    实测 26 条全量送进 deepseek-v4.1-flash 会超时。
    """
    from app.conversation.memory_hygiene import is_punctually_stale
    from app.models.service_memory import is_fact_invalidated

    now = datetime.now(timezone.utc)
    usable = [
        f for f in facts
        if not is_fact_invalidated(f, now) and not is_punctually_stale(f, now)
    ]
    usable.sort(key=lambda f: f.last_referenced_at, reverse=True)
    usable = usable[: settings.reflection_max_evidence]
    lines: list[str] = []
    index_to_fid: dict[int, str] = {}
    for i, fact in enumerate(usable, start=1):
        lines.append(f"{i}. {fact.content}")
        index_to_fid[i] = fact.fid
    return "\n".join(lines), index_to_fid


def validate_insight(
    raw: object,
    index_to_fid: dict[int, str],
    min_evidence: int,
) -> MemoryInsight | None:
    """grounding 硬校验：来源不足或引用了不存在的编号则整条丢弃。

    这里宁缺毋滥——被丢弃的错误归纳最多让角色少说一句判断，
    而被接纳的幻觉会长期驻留在人格里。
    """
    if not isinstance(raw, dict):
        return None
    content = str(raw.get("content", "")).strip()
    if not content:
        return None
    # 模型在归纳时也会滑回第三人称（实测产出过"对方对用户…"），
    # 洞见常驻 system 前缀，出戏写法的影响比单条事实更大
    from app.conversation.memory_hygiene import normalize_referent

    content, _ = normalize_referent(content)
    kind = str(raw.get("kind", "trend")).strip().lower()
    if kind not in _VALID_KINDS:
        kind = "trend"
    evidence = raw.get("evidence")
    if not isinstance(evidence, list):
        return None
    fids: list[str] = []
    for item in evidence:
        try:
            idx = int(item)
        except (TypeError, ValueError):
            continue
        fid = index_to_fid.get(idx)
        if fid and fid not in fids:
            fids.append(fid)
    if len(fids) < min_evidence:
        logger.info(
            "反思洞见被丢弃（来源不足 %d<%d）: %s", len(fids), min_evidence, content[:30]
        )
        return None
    return MemoryInsight(
        fid=new_fact_id(), content=content[:60], kind=kind,  # type: ignore[arg-type]
        source_fids=fids,
    )


def render_insight_lines(service_memory: object | None) -> list[str]:
    """取常驻注入的洞见文本（引擎与主动关怀调度器共用同一口径）。

    不带时间戳、不解释来源：注入段位于 system 前缀，任何逐轮变化的内容都会击穿
    prompt 前缀缓存；来源信息留给后台审计页展示，不进提示词。
    """
    if not settings.memory_reflection_enabled or service_memory is None:
        return []
    insights = list(getattr(service_memory, "insights", []) or [])
    live = [i for i in insights if not i.invalidated]
    return [i.content for i in live[: settings.reflection_max_insights]]


async def generate_insights(
    facts: list[MemoryFact],
) -> list[MemoryInsight]:
    """执行一次反思，返回通过 grounding 校验的洞见（可能为空）。"""
    evidence_text, index_to_fid = build_evidence_block(facts)
    if not evidence_text:
        return []
    prompt = _REFLECT_SYSTEM_PROMPT.format(
        min_evidence=settings.reflection_min_evidence,
        max_insights=settings.reflection_max_insights,
    )
    # 用 summary 任务而非 focus：focus 的超时是 20s（为短输出抽取而设），
    # 反思是长输出归纳，实测同一提示词在 focus 通道上必超时
    provider = get_llm_provider("summary")
    try:
        raw_response = await provider.complete(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": evidence_text},
            ],
            # 不复用 focus_temperature：那是为"抽取"调的 0.0，
            # 反思要的是跨条归纳，温度 0 下推理型模型会直接返回空串（实测）。
            # max_tokens 同理不能沿用 focus_max_tokens=384，几条带 evidence 数组的
            # JSON 对象就会被截断成无法解析的半截。
            temperature=settings.reflection_temperature,
            max_tokens=settings.reflection_max_tokens,
            # 逐调用超时：反思是后台归纳，沿用任务默认 20-30s 会被推理耗时撞断
            # （实测同一提示词在 focus/summary 通道上均以超时收场）
            timeout=settings.reflection_timeout_seconds,
            response_format={"type": "json_object"},
        )
    except Exception as exc:  # 反思是锦上添花，失败绝不影响对话主链路
        logger.warning("反思调用失败，本轮跳过: %s", exc)
        return []

    from app.conversation.focus_tracker import FocusTracker

    parsed = FocusTracker._parse_response(raw_response)
    if not parsed:
        # 区分"模型没回"与"回了但解析不了"：前者是额度/超时，后者是提示词漂移，
        # 混成一条日志会让线上排查时无从下手
        logger.warning(
            "反思响应无法解析（原始长度 %d）: %s",
            len(raw_response or ""), (raw_response or "")[:120],
        )
        return []
    items = parsed.get("insights")
    if not isinstance(items, list):
        return []
    insights: list[MemoryInsight] = []
    for item in items[: settings.reflection_max_insights]:
        insight = validate_insight(item, index_to_fid, settings.reflection_min_evidence)
        if insight is not None:
            insights.append(insight)
    return insights
