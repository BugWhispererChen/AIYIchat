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
# shingle 归一化：标点与 emoji 不参与切块（换一套 emoji 不该让复读"洗白"，也不该因标点误判）
_SHINGLE_STRIP_RE = re.compile(r"[\W_]+", re.UNICODE)

# 公共前缀至少要这么多字才当作"复读前兆"（低于此长度容易把"好的""嗯"这类开头误判成照抄）
MIN_PREFIX_REPEAT_CHARS = 16
# 复述式复读的字符切块大小：5~6 字一块，够长才不会把常用短语算成重复
SHINGLE_SIZE = 6


def _shingles(text: str, size: int = SHINGLE_SIZE) -> set[str]:
    """字符 n-gram 集合；去掉标点与 emoji，只比可读内容的重复程度。"""
    cleaned = _SHINGLE_STRIP_RE.sub("", text or "")
    if len(cleaned) < size:
        return {cleaned} if cleaned else set()
    return {cleaned[i : i + size] for i in range(len(cleaned) - size + 1)}


def shingle_coverage(new_text: str, recent_assistant_texts: list[str]) -> float:
    """新内容里有多大比例是旧回复说过的（换序复述也能抓到）。

    为什么不能只用公共前缀或整段相似度：实测同一话题连问四次时，
    真正"复读"的那一轮与上一轮的 6-gram 覆盖率约 0.58，而正常换述只有 0.23、
    完全无关是 0.00；逐字照抄用前缀判即可，但"把上句原封不动挪到新句中间"这类
    复述前缀很短、整段 difflib 也只有 0.67，两种老尺子都漏。
    用"新内容被旧内容覆盖的比例"而非 Jaccard，是因为新回复通常更长，
    Jaccard 会被长度差稀释（同一样本 Jaccard 只有 0.34）。
    """
    new_set = _shingles(new_text)
    if not new_set:
        return 0.0
    best = 0.0
    for old in recent_assistant_texts:
        old_set = _shingles(old)
        if not old_set:
            continue
        best = max(best, len(new_set & old_set) / len(new_set))
    return best


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
