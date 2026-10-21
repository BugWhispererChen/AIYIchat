"""消息拆分器：将 LLM 输出的带分隔符回复拆分为多条消息。

拟人化多消息回复的核心组件：
- LLM 在 Prompt 引导下使用 ||| 分隔符标记消息边界
- 本模块解析分隔符，将完整回复拆分为独立的消息列表
- 兜底策略：LLM 未使用分隔符但自然按空行分段时，同样拆分多条
- 适配器层逐条发送，模拟真人聊天的分段发送效果

``split_for_pacing`` 是在上述结果之上再做"节奏分条"：把仍然过长的单条消息按句末边界
切成适合逐条发送的小块。切分要避开代码围栏、ASCII 表与编号列表，否则会把代码块或
"1. 2. 3." 从中间劈开——真人连发不会打断一份清单。
"""

import re
from dataclasses import dataclass

# LLM 输出中标记消息边界的分隔符
MESSAGE_DELIMITER = "|||"

# 节奏分条默认阈值：单条超过此长度才考虑再切（短消息连发反而不像人）
PACING_AUTO_SPLIT_THRESHOLD = 120
# 节奏分条目标块长度：切到接近这个字数即收口
PACING_TARGET_CHUNK_CHARS = 60
# 小于此字数的碎片回并进相邻块，避免"嗯"/"好"这种无意义独立气泡
PACING_MIN_CHUNK_CHARS = 12

# 代码围栏（含未闭合的半个围栏）：内部一律不切
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
# 表格/ASCII 图特征：连续 3 个以上竖线或加号横线的行，整行视为一块
_ASCII_BLOCK_LINE_RE = re.compile(r"^\s*[|+].{2,}$", re.MULTILINE)
# 句末边界：中文句号/叹号/问号/省略号与换行。省略号按多点合并处理
_SENTENCE_END_RE = re.compile(r"(?<=[。！？!?…])(?=\S)|\n+")
# 编号/列表项起始，切在此处会让清单被劈成两半
_LIST_ITEM_RE = re.compile(r"^\s*(?:\d+[.)、]|[·\-•])\s")


@dataclass(frozen=True)
class SplitResult:
    """拆分结果。"""

    messages: list[str]  # 拆分后的消息列表
    was_split: bool  # 是否实际发生了拆分


def split_messages(text: str, max_messages: int = 4) -> SplitResult:
    """将含分隔符的文本拆分为多条消息。

    拆分规则（优先级从高到低）：
    1. 按 ||| 显式分隔符拆分（Prompt 引导 LLM 输出）
    2. 无分隔符但含空行分段时，按空行拆分（LLM 自然分段兑底）
    3. 每条消息 strip 后非空才保留，相邻完全相同的分段去重（防 LLM 复读）
    4. 最多保留 max_messages 条；超出部分**回并到最后一条**而不是丢弃，
       否则回复尾部会被静吞（既少发给对方，也没落库）
    5. 无法拆分时返回原始文本作为单条消息

    Args:
        text: LLM 输出的完整回复文本（可能含 ||| 分隔符）
        max_messages: 最大允许的消息条数

    Returns:
        SplitResult 包含消息列表和是否拆分的标记
    """
    # 1. 显式分隔符拆分
    if MESSAGE_DELIMITER in text:
        parts = text.split(MESSAGE_DELIMITER)
        messages = _dedup_adjacent([p.strip() for p in parts if p.strip()])
        return SplitResult(
            messages=_limit(messages, max_messages),
            was_split=len(messages) > 1,
        )

    # 2. 兑底：无分隔符但含空行分段（LLM 自然分段行为，同样规为多条消息）
    if "\n\n" in text:
        parts = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
        parts = _dedup_adjacent(parts)
        if len(parts) > 1:
            return SplitResult(
                messages=_limit(parts, max_messages), was_split=True
            )

    return SplitResult(messages=[text], was_split=False)


def split_for_pacing(
    text: str,
    max_messages: int = 5,
    *,
    auto_split_threshold: int = PACING_AUTO_SPLIT_THRESHOLD,
    target_chunk_chars: int = PACING_TARGET_CHUNK_CHARS,
    min_chunk_chars: int = PACING_MIN_CHUNK_CHARS,
) -> list[str]:
    """为"逐条发送节奏"准备的消息列表。

    先走 ``split_messages`` 的显式分隔符与空行规则；对仍然超长的单条消息，按句末
    边界继续切到接近 ``target_chunk_chars``，并避开代码围栏、ASCII 表与编号列表。

    与 ``split_messages`` 的区别：本函数只关心"怎么切得像人连发"，条数上限统一在
    最后回并处理，返回值直接是消息列表（无 was_split 标记），供适配器与流式闸门共用。
    """
    base = split_messages(text, max_messages=max_messages).messages
    base = [m for m in base if m.strip()]
    if not base:
        return []
    out: list[str] = []
    for msg in base:
        if len(msg) <= auto_split_threshold:
            out.append(msg)
            continue
        out.extend(_split_long(msg, target_chunk_chars, min_chunk_chars))
    return _limit(out, max_messages)


def _split_long(
    text: str, target_chunk_chars: int, min_chunk_chars: int
) -> list[str]:
    """把一条超长消息按句末边界切成多块，保护代码围栏/ASCII 表/编号列表。

    做法：先取全部句末切点，剔除落在保护区内的切点，再贪心累加——累计长度达到目标
    就在当前切点收口。切点之外绝不硬切，因此宁可得出一块略长的消息，也不把代码块或
    清单从中间劈开。
    """
    protected = _protected_spans(text)
    cuts = sorted(
        {
            m.end()
            for m in _SENTENCE_END_RE.finditer(text)
            if not _in_spans(m.end(), protected) and 0 < m.end() < len(text)
        }
    )
    chunks: list[str] = []
    start = 0
    for cut in cuts:
        if cut - start >= target_chunk_chars:
            chunks.append(text[start:cut].strip())
            start = cut
    chunks.append(text[start:].strip())
    chunks = [c for c in chunks if c]
    if not chunks:
        return [text]
    # 碎片回并：过短的块贴给前一块，避免"嗯"这种无意义独立气泡
    merged: list[str] = [chunks[0]]
    for c in chunks[1:]:
        if len(c) < min_chunk_chars:
            merged[-1] = f"{merged[-1]}\n{c}"
        else:
            merged.append(c)
    return merged


def _in_spans(point: int, spans: list[tuple[int, int]]) -> bool:
    """判断切点是否落在任一保护区间内。"""
    return any(start <= point < stop for start, stop in spans)


def _protected_spans(text: str) -> list[tuple[int, int]]:
    """收集不可切分的字符区间：成对代码围栏、ASCII 表行、编号列表项所在段。"""
    spans = [(m.start(), m.end()) for m in _FENCE_RE.finditer(text)]
    # 未闭合围栏（模型输出被截断）：从最后一个 ``` 到文末整段保护
    if text.count("```") % 2 == 1:
        last = text.rfind("```")
        if last >= 0:
            spans.append((last, len(text)))
    ascii_lines = list(_ASCII_BLOCK_LINE_RE.finditer(text))
    if ascii_lines:
        spans.append((ascii_lines[0].start(), ascii_lines[-1].end()))
    items = list(_LIST_ITEM_RE.finditer(text))
    if items:
        spans.append((items[0].start(), len(text)))
    return spans


def _limit(messages: list[str], max_messages: int) -> list[str]:
    """条数护栏：超出上限时把多出的内容回并到最后一条，绝不丢弃。"""
    if max_messages <= 0 or len(messages) <= max_messages:
        return messages
    kept = messages[: max_messages - 1]
    overflow = "\n".join(messages[max_messages - 1 :])
    return kept + [overflow]


def _dedup_adjacent(parts: list[str]) -> list[str]:
    """去除相邻完全相同的分段（防 LLM 输出重复段被逐条发送）。"""
    return [p for i, p in enumerate(parts) if i == 0 or p != parts[i - 1]]
