"""记忆治理：指称归一与点状事实时效（纯规则，零 LLM 调用）。

针对 2026-10-23 测试服取证的两处漏点：

1. **指称污染人格**：存量事实以第三人称记述对方与角色（如"用户与AI关系亲密，
   互称老公/老婆"）。注入后角色读到的是"我和 AI 关系亲密"，等于把自曝非人写进了
   长期记忆；术语改名「对方」只作用于新写入，存量数据仍会长期生效。
2. **点状事实不过期**：抽取器按提示词把"明天要面试"写成带绝对日期的
   "对方8月26日有面试"。这类陈述描述的是**未来某一时刻的状态**，时刻一过即成历史；
   但 `validity_downweight` 只降权不剔除，而 `last_referenced_at` 每轮被注入刷新，
   于是"越记越牢"，角色会在几个月后仍把过期安排当作近况关心。

时效判定只在**写入时刻**打标记（`MemoryFact.punctual`）：只有当事件日期严格晚于
它出自的那条消息的时刻，才说明这句话当时是在讲未来。同一天发生的事
（"10月21日被领导批评"）不打标记，避免把情景记忆当成过期垃圾清掉——
误删一段真实经历的代价高于多留一条陈旧事实。
"""

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings
from app.models.service_memory import MemoryFact

logger = logging.getLogger(__name__)

# 指称改写表：按声明顺序逐条替换（长串在前，避免"与AI"被单字规则截断）
_REFERENT_REWRITES: tuple[tuple[str, str], ...] = (
    ("与AI", "与我"),
    ("和AI", "与我"),
    ("跟AI", "与我"),
    ("对AI", "对我"),
    ("向AI", "向我"),
    ("被AI", "被我"),
    ("请AI", "请我"),
    ("让AI", "让我"),
    # 只对"把 AI 当作对话对象"的动词生效。严禁做成泛化的 AI→我：
    # 存量里存在"将AI源代码上传GitHub"这类条目，AI 指的是产品本身而非角色。
    ("称呼AI", "称呼我"),
    ("称AI为", "称我为"),
    ("伤害AI", "伤害我"),
    ("惹AI", "惹我"),
    ("陪AI", "陪我"),
    ("安慰AI", "安慰我"),
    ("告诉AI", "告诉我"),
    ("发给AI", "发给我"),
    ("AI也", "我也"),
)

# 违规指称检测：出现"用户"/"身份"作为记述主语，或残留"AI"作为关系对象
_DIRTY_REFERENT_RE = re.compile(r"(用户|身份)|([与和跟对向被请让]AI)")

# 点状事实的宽限期（天）：事件时刻过后多久判为不再成立
_DEFAULT_GRACE_DAYS = 2.0


def new_fact_id() -> str:
    """记忆条目稳定 ID（反思层洞见需按 ID 回链来源，content 不是主键）。"""
    return uuid.uuid4().hex[:12]


def is_referent_dirty(content: str) -> bool:
    """事实文本是否仍带第三人称/出戏指称（用于治理尺子计数与迁移筛选）。"""
    return _DIRTY_REFERENT_RE.search(content) is not None


def normalize_referent(content: str) -> tuple[str, bool]:
    """把事实文本中的第三方指称改写为角色第一人称视角。

    返回 (改写后文本, 是否发生改写)。只做确定性替换，不改语义、不缩写。
    """
    text = content
    for old, new in _REFERENT_REWRITES:
        text = text.replace(old, new)
    # 记述主语归一：仅当"用户/身份"用作主语前缀或所有格时才替换，
    # 避免把正文里可能出现的"用户协议"这类名词误改（本域几乎不发生，仍留余地）
    text = re.sub(r"^用户(?=[^协账户])", "对方", text)
    text = re.sub(r"^身份(?=[^证份])", "对方", text)
    return text, text != content


def stamp_punctual(fact: MemoryFact, source_message_at: datetime | None) -> bool:
    """在写入时刻判定该事实是否为"点状未来状态"，并就地打标。

    判定依据只能是写入时刻：事后无法区分"当时说明天"与"当时说昨天"。
    `valid_at` 严格晚于消息时刻 → 那句话当时讲的是未来 → 过期后应当失效。
    返回是否打标（便于日志与测试断言）。
    """
    if fact.valid_at is None or source_message_at is None:
        fact.punctual = False
        return False
    valid_at = _aware_utc(fact.valid_at)
    msg_at = _aware_utc(source_message_at)
    fact.punctual = valid_at > msg_at
    return fact.punctual


def punctual_expiry_at(
    fact: MemoryFact, now: datetime | None = None
) -> datetime | None:
    """点状事实的失效时刻（现实成立轴）；不该失效返回 None。

    仅对 `punctual=True` 且 `valid_at` 已过去超过宽限期的事实给出时点，
    供读取路径判陈旧、写入路径落 `invalid_at` 共用同一口径（避免两份规则漂移）。
    """
    if not fact.punctual or fact.valid_at is None:
        return None
    if now is None:
        now = datetime.now(timezone.utc)
    grace = float(getattr(settings, "memory_punctual_grace_days", _DEFAULT_GRACE_DAYS))
    expiry = _aware_utc(fact.valid_at) + timedelta(days=grace)
    return expiry if _aware_utc(now) > expiry else None


def is_punctually_stale(fact: MemoryFact, now: datetime | None = None) -> bool:
    """读取路径的陈旧判定：只判内存对象，不写库。"""
    return punctual_expiry_at(fact, now) is not None


# 改述判定阈值：字符二元组 Jaccard。用测试服真实数据标定——
# 同一事件的两条改述（"…开会时被领导批评" vs "…开会被领导批评且感到委屈"）
# 实测 0.652，而其余任意两条事实两两比对均 < 0.25，故取 0.45 留足两侧余量。
_RESTATEMENT_JACCARD = 0.45


def _bigrams(text: str) -> set[str]:
    """去空白后的字符二元组集合（中文无需分词即可取得稳定的重叠度量）。"""
    s = "".join(text.split())
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def is_restatement(new_content: str, old_content: str) -> bool:
    """新事实是否为已有事实的**改述/合并重述**（同一件事换个说法再讲一遍）。

    冲突消解只挑"最相似的一条"比对，一条把两段既有记忆合并重述的新事实
    与任何单条的相似度都不足以进 `_SIM_DEDUP`，于是被当作全新信息 ADD，
    库里便出现"事件 + 评价 + 事件并评价"三条并存。改述判定独立于向量可用性，
    因为最该被拦住的正是无向量环境（`embedding_enabled=False` 的默认部署）。
    """
    a, b = _bigrams(new_content), _bigrams(old_content)
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= _RESTATEMENT_JACCARD


def plan_temporal_refresh(
    fact: MemoryFact, now: datetime | None = None
) -> datetime | None:
    """迁移/落库用：给出该补写的 `invalid_at`，无需补写返回 None。

    已带 `invalid_at`/`expired_at` 的事实交由既有硬过滤处理，不在此重复判定。
    """
    if fact.invalid_at is not None or fact.expired_at is not None:
        return None
    return punctual_expiry_at(fact, now)


def audit_memory(
    facts: list[MemoryFact], now: datetime | None = None
) -> dict[str, Any]:
    """记忆体检计数（治理尺子与迁移脚本共用同一实现，保证期望值不漂移）。"""
    if now is None:
        now = datetime.now(timezone.utc)
    dirty = [f.content for f in facts if is_referent_dirty(f.content)]
    stale = [f.content for f in facts if is_punctually_stale(f, now)]
    contents = [f.content for f in facts]
    return {
        "total": len(facts),
        "dirty_referent": len(dirty),
        "punctual_stale": len(stale),
        "exact_duplicate": len(contents) - len(set(contents)),
        "dirty_samples": dirty[:3],
        "stale_samples": stale[:3],
    }


def _aware_utc(dt: datetime) -> datetime:
    """归一为 aware UTC（存量数据可能无时区信息，naive 一律按 UTC 解释）。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
