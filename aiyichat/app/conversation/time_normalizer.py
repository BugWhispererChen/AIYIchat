"""jionlp 相对时间归一化封装（P2）。

硬约束（方案 §P2-15）：
- 惰性导入 + try/except 全局降级：jionlp 缺失/导入失败时 _JIONLP_AVAILABLE=False，
  全部方法返回 None/空，链路退化为纯 LLM 抽取（现状行为），绝不阻断主链路
- 每次调用必须显式传 time_base：jionlp 官方文档明示不传会卡死在初次启动日期
- 解析抛 ValueError 一律静默降级返回 None，交 LLM 兜底
- 不支持的表达（"前两天"/"昨晚1点"/"二十几天"）原样保留，不归一化、不报错

time_base 约定：传「用户本地 naive wall-clock」（jionlp 输出无时区的本地时间串），
解析结果再按 ctx.tz 转 aware UTC 落库。
"""

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# 惰性导入标记：None=未尝试，True=可用，False=不可用（导入失败/版本不兼容）
_JIONLP_AVAILABLE: bool | None = None
_jio: Any = None


def _load_jionlp() -> bool:
    """惰性导入 jionlp，失败即全局降级。

    Returns:
        True 表示可用，False 表示不可用（后续调用直接返回 None，不再重试）
    """
    global _JIONLP_AVAILABLE, _jio
    if _JIONLP_AVAILABLE is not None:
        return _JIONLP_AVAILABLE
    try:
        import jionlp as jio  # type: ignore[import-untyped]  # noqa: PLC0415
        _jio = jio
        _JIONLP_AVAILABLE = True
        logger.info("jionlp 加载成功，相对时间归一化已启用")
    except Exception as e:
        _JIONLP_AVAILABLE = False
        logger.warning("jionlp 加载失败，相对时间归一化降级为纯 LLM: %s", e)
    return _JIONLP_AVAILABLE


def _to_naive_local(base: datetime, tz: timezone) -> datetime:
    """将 aware UTC 时间转为指定时区的 naive 本地时间（jionlp 的 time_base 要求）。

    jionlp 输出无时区的本地时间串，因此 time_base 必须是用户本地 naive wall-clock，
    否则解析结果会整体偏移。
    """
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return base.astimezone(tz).replace(tzinfo=None)


def extract_time_spans(
    text: str, base: datetime, tz: timezone
) -> list[dict]:
    """从文本中抽取时间实体（jionlp extract_time 的封装）。

    Args:
        text: 待抽取文本
        base: 时间基点（aware UTC），内部转为 tz 对应的 naive 本地时间
        tz: 用户本地时区

    Returns:
        时间实体列表，每项含 text/offset/type/detail；失败或不可用时返回空列表
    """
    if not _load_jionlp():
        return []
    try:
        naive_base = _to_naive_local(base, tz)
        result = _jio.ner.extract_time(text, time_base=naive_base)  # type: ignore[union-attr]
        return result if isinstance(result, list) else []
    except ValueError:
        # 不支持的表达（如"前两天"在某些版本抛 ValueError）：静默降级
        return []
    except Exception as e:
        logger.warning("jionlp extract_time 异常，降级为空: %s", e)
        return []


def normalize_relative(
    text: str, base: datetime, tz: timezone
) -> str | None:
    """将相对时间表达归一化为绝对日期串（YYYY-MM-DD）。

    命中单个 time_point 且 definition=="accurate" 时返回其起始日期；
    否则（无命中/模糊/跨度）返回 None，交 LLM 兜底。

    Args:
        text: 待归一化文本（如"下周三"）
        base: 时间基点（aware UTC）
        tz: 用户本地时区

    Returns:
        绝对日期串（YYYY-MM-DD），无法归一化时返回 None
    """
    spans = extract_time_spans(text, base, tz)
    if not spans:
        return None
    # 只接受单个准确时间点：模糊/跨度/多时间点不归一化，避免误改
    if len(spans) != 1:
        return None
    span = spans[0]
    detail = span.get("detail", {})
    if detail.get("type") != "time_point" or detail.get("definition") != "accurate":
        return None
    time_range = detail.get("time", [])
    if not time_range or not isinstance(time_range, list):
        return None
    start_str = time_range[0]  # "2026-09-09 15:00:00"
    try:
        # jionlp 输出 naive 本地时间串，取日期部分
        naive = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")
        return naive.strftime("%Y-%m-%d")
    except (ValueError, IndexError):
        return None


def parse_time_point(
    text: str, base: datetime, tz: timezone
) -> datetime | None:
    """解析文本为 aware UTC 时间点（jionlp parse_time 的封装）。

    用于 _save_events 的 due_at 解析：能正确处理"下周三""9月15号"等表达。

    Args:
        text: 待解析文本
        base: 时间基点（aware UTC）
        tz: 用户本地时区

    Returns:
        aware UTC 时间点，解析失败/不可用时返回 None
    """
    if not _load_jionlp():
        return None
    try:
        naive_base = _to_naive_local(base, tz)
        result = _jio.parse_time(text, time_base=naive_base)  # type: ignore[union-attr]
        if not isinstance(result, dict):
            return None
        if result.get("type") != "time_point":
            return None
        time_range = result.get("time", [])
        if not time_range:
            return None
        # 取起始时间，按本地时区转 aware UTC
        naive = datetime.strptime(time_range[0], "%Y-%m-%d %H:%M:%S")
        return naive.replace(tzinfo=tz).astimezone(timezone.utc)
    except ValueError:
        return None
    except Exception as e:
        logger.warning("jionlp parse_time 异常，降级为 None: %s", e)
        return None


def verify_llm_date(
    claim: str, source_text: str, base: datetime, tz: timezone
) -> bool:
    """校验 LLM 给出的日期是否真能从原文推出（防幻觉）。

    用 extract_time 检查 source_text 是否真含与 claim 对应的时间实体：
    - 原文含该日期 → True
    - 原文不含任何时间实体 → True（LLM 可能基于上下文合理推断，不武断否决）
    - 原文含时间实体但与 claim 不符 → False（LLM 编造日期）

    Args:
        claim: LLM 给出的日期串（YYYY-MM-DD）
        source_text: 原始对话文本
        base: 时间基点（aware UTC）
        tz: 用户本地时区

    Returns:
        True 表示可信（或无法证伪），False 表示证伪
    """
    if not _load_jionlp():
        return True  # 无校验能力时默认信任 LLM
    try:
        claim_date = datetime.strptime(claim, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return True  # claim 本身非法，由调用方按 None 处理
    spans = extract_time_spans(source_text, base, tz)
    if not spans:
        return True  # 原文无时间实体，无法证伪
    # 原文含时间实体：检查是否有任一 span 的日期与 claim 匹配
    for span in spans:
        detail = span.get("detail", {})
        time_range = detail.get("time", [])
        if not time_range:
            continue
        try:
            span_date = datetime.strptime(time_range[0], "%Y-%m-%d %H:%M:%S").date()
            if span_date == claim_date:
                return True
        except (ValueError, IndexError):
            continue
    # 原文含时间实体但均不匹配 → 证伪
    return False
