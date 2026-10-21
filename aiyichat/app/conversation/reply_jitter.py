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

# 公共前缀至少要这么多字才当作"复读前兆"（低于此长度容易把"好的""嗯"这类开头误判成照抄）
MIN_PREFIX_REPEAT_CHARS = 16


def normalize_text(text: str) -> str:
    """文本归一化：strip + 连续空白折叠为单空格。"""
    return _WHITESPACE_RE.sub(" ", text.strip())


def prefix_repeat_ratio(new_text: str, recent_assistant_texts: list[str]) -> float:
    """已放行前缀与最近某条回复的"复读前兆"比例（流式专用）。

    整段相似度 ``is_repeat_reply`` 要等全文才能算，而流式链路必须**边发边判**，
    否则复读已经全显示在屏幕上再判定就晚了。真复读的特征是新文本逐字照抄旧回复的开头，
    因此用"公共前缀占旧回复长度的比例"做早期信号：模型越是照抄，这个比值越接近 1，
    且不需要等全文。非复读的改写回复公共前缀很快就断开，比值天然低。

    返回所有历史回复中的最大值；不足 ``min_prefix_chars`` 的公共前缀不计（避免
    "好的""嗯"这类开头就误判）。
    """
    normalized_new = normalize_text(new_text)
    if not normalized_new:
        return 0.0
    best = 0.0
    for old in recent_assistant_texts:
        normalized_old = normalize_text(old)
        if not normalized_old:
            continue
        limit = min(len(normalized_new), len(normalized_old))
        shared = 0
        while shared < limit and normalized_new[shared] == normalized_old[shared]:
            shared += 1
        if shared < MIN_PREFIX_REPEAT_CHARS:
            continue
        best = max(best, shared / len(normalized_old))
    return best


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
