"""人设一致性自检。

对 LLM 回复做轻量规则校验，拦截角色越界（自称 AI/语言模型）与禁用词泄漏，
必要时做温和清洗（剔除越界句）。纯规则实现，由 consistency_check_enabled 开关控制。
"""

import logging
import re

logger = logging.getLogger(__name__)

# 角色越界短语（自曝 AI 身份）
_ROLE_BREAK_PATTERNS: list[str] = [
    r"作为(一个)?(AI|人工智能|语言模型|大模型|机器人|智能助手|助手|程序)",
    r"我是(一个)?(AI|人工智能|语言模型|大模型|机器人|程序|智能助手|聊天机器人)",
    r"我(只是|不过是|其实是)(一个)?(AI|程序|机器|模型)",
    r"as an AI",
    r"language model",
    r"I am an AI",
]


class ConsistencyChecker:
    """人设一致性校验器。"""

    def __init__(self) -> None:
        self._role_break_re = re.compile(
            "|".join(_ROLE_BREAK_PATTERNS), re.IGNORECASE
        )

    def check(
        self, text: str, forbidden_words: list[str] | None = None
    ) -> tuple[bool, list[str]]:
        """校验回复文本，返回 (是否通过, 违规原因列表)。"""
        issues: list[str] = []
        if not text:
            return True, issues
        if self._role_break_re.search(text):
            issues.append("role_break")
        if forbidden_words:
            hit = [w for w in forbidden_words if w and w in text]
            if hit:
                issues.append("forbidden:" + ",".join(hit[:3]))
        return len(issues) == 0, issues

    def scrub(self, text: str) -> str:
        """剔除含角色越界短语的句子；若全部被剔除则退回原文避免空回复。"""
        if not text:
            return text
        sentences = re.split(r"(?<=[。！？!?\n])", text)
        kept = [s for s in sentences if not self._role_break_re.search(s)]
        result = "".join(kept).strip()
        return result or text
