"""消息拆分器：将 LLM 输出的带分隔符回复拆分为多条消息。

拟人化多消息回复的核心组件：
- LLM 在 Prompt 引导下使用 ||| 分隔符标记消息边界
- 本模块解析分隔符，将完整回复拆分为独立的消息列表
- 兜底策略：LLM 未使用分隔符但自然按空行分段时，同样拆分多条
- 适配器层逐条发送，模拟真人聊天的分段发送效果
"""

import re
from dataclasses import dataclass

# LLM 输出中标记消息边界的分隔符
MESSAGE_DELIMITER = "|||"


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
    4. 最多保留 max_messages 条（防止 LLM 失控生成过多消息）
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
        if len(messages) > max_messages:
            messages = messages[:max_messages]
        return SplitResult(messages=messages, was_split=len(messages) > 1)

    # 2. 兑底：无分隔符但含空行分段（LLM 自然分段行为，同样规为多条消息）
    if "\n\n" in text:
        parts = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
        parts = _dedup_adjacent(parts)
        if len(parts) > 1:
            if len(parts) > max_messages:
                parts = parts[:max_messages]
            return SplitResult(messages=parts, was_split=True)

    return SplitResult(messages=[text], was_split=False)


def _dedup_adjacent(parts: list[str]) -> list[str]:
    """去除相邻完全相同的分段（防 LLM 输出重复段被逐条发送）。"""
    return [p for i, p in enumerate(parts) if i == 0 or p != parts[i - 1]]
