"""Token 数量启发式估算（仅用于调用前余额预检）。

计费结算以 LLM API 返回的真实 usage 为准；本模块仅在调用前粗略估算
输入文本的 token 量，用于拦截余额明显不足的请求，避免无效的 LLM 调用。

估算系数：CJK 字符约 1.5 token/字，其他字符约 0.35 token/字符，
与各后端分词器均有偏差，预检场景可接受。
"""

import math


def estimate_tokens(text: str) -> int:
    """估算文本的 token 数（向上取整，最小为 1）。

    Args:
        text: 待估算文本

    Returns:
        估算的 token 数；空文本返回 1（避免预检门槛为 0）
    """
    if not text:
        return 1
    cjk = sum(1 for ch in text if _is_cjk(ch))
    other = len(text) - cjk
    return max(1, math.ceil(cjk * 1.5 + other * 0.35))


def _is_cjk(ch: str) -> bool:
    """判断字符是否属于 CJK（中日韩）文字范围。"""
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF  # 基本汉字
        or 0x3400 <= code <= 0x4DBF  # 扩展 A
        or 0xF900 <= code <= 0xFAFF  # 兼容汉字
        or 0x3000 <= code <= 0x303F  # CJK 标点
        or 0xFF00 <= code <= 0xFFEF  # 全角符号
    )
