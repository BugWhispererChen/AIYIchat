"""回复抖动检测：拦截模型复读上一条回复的异常输出。

故障背景：弱输入（如 [表情]、重复话题图片）可能触发 LLM 从上下文中原样
复读上一条 assistant 回复，身份侧表现为"一条消息发三遍"。
本模块在引擎后处理阶段比对新回复与最近 assistant 消息的相似度，
命中后由引擎追加提示重试一次（重试逻辑见 engine.handle_message）。
"""

import difflib
import re

# 空白归一化：连续空白折叠为单个空格，避免排版差异影响相似度
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """文本归一化：strip + 连续空白折叠为单空格。"""
    return _WHITESPACE_RE.sub(" ", text.strip())


def is_repeat_reply(
    new_text: str,
    recent_assistant_texts: list[str],
    threshold: float = 0.9,
) -> bool:
    """判断新回复是否与最近的 assistant 回复高度雷同（复读）。

    Args:
        new_text: 本轮待发送的完整回复文本
        recent_assistant_texts: 最近的 assistant 消息内容（按时间升序）
        threshold: 相似度阈值（0~1），达到即判定为复读

    Returns:
        True 表示命中复读，应触发重试
    """
    normalized_new = normalize_text(new_text)
    if not normalized_new:
        return False
    for old in recent_assistant_texts:
        normalized_old = normalize_text(old)
        if not normalized_old:
            continue
        ratio = difflib.SequenceMatcher(
            None, normalized_new, normalized_old
        ).ratio()
        if ratio >= threshold:
            return True
    return False
