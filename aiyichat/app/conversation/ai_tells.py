"""中文"AI 腔"确定性扫描器。

动机：角色回复里最出戏的不是错话，而是那些一眼假的句式——"当然可以""综上所述"
"希望对你有帮助""随着……的发展"。这类痕迹靠提示词约束不可靠（模型照样写），
靠 LLM 自检又要多花一次调用。本模块用一组确定性正则做**发送前门禁**：
命中 blocking 档的句子直接剔除，命中 review 档只计数供评测观察，零模型成本。

档位设计参照社区开源工具 anti-ai-writing-kit-zh 的 scan-writing.mjs
（22 条带级别规则 + 词表 + blocking=0 才算通过），但规则清单与正则按本项目
"只删能整句删掉的助手腔"重新编写，并由 test_ai_tells.py 逐条验证误删边界。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 句子切分：句末标点**连跑算一个边界**（"!!!" 不能被切成三句，否则 multi_exclaim 永远命中不了）
_SENTENCE_TAIL_RE = re.compile(r"[。！？!?…]+[”』\"']?|\n+")


def _split_sentences(text: str) -> list[str]:
    """按句末边界切句，标点连跑与其后的收尾引号归属前一句。"""
    parts: list[str] = []
    start = 0
    for m in _SENTENCE_TAIL_RE.finditer(text):
        parts.append(text[start : m.end()])
        start = m.end()
    if start < len(text):
        parts.append(text[start:])
    return [p for p in parts if p]

# 引号内的命中降级：角色复述对方原话不是 AI 腔
_QUOTED_RE = re.compile("[“\"'『「][^”\"'』」]{1,60}[”\"'』」]")

# "这句有没有内容"的判定。emoji 也算内容：真人报喜会单独甩一条 🎉，
# 把它当无字空句吞掉，等于在最该有情绪反应的地方把味道裁掉
# （长度治理实测：整轮字数 76→18 的同时 emoji 命中率 11/13→6/13，"升职"被回成书面祝贺）。
_TEXTUAL_RE = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")
_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]")

# —— blocking：一眼假的助手腔，删掉不影响角色在说什么 ——
_BLOCKING_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "assistant_opener",
        re.compile(
            r"^(?:好的[，,]?)?(?:当然可以|当然没问题|当然啦|没问题|好问题"
            r"|这是(?:一个)?(?:很好的|好的|好)?问题"
            r"|你说得(?:很)?对|你观察得(?:很)?敏锐|你的看法(?:很|挺)(?:有道理|敏锐))"
            r"[。！!，,]?$"
        ),
    ),
    (
        "assistant_closer",
        re.compile(
            r"^希望[^。！？]{0,12}(?:帮助|帮到你|对你有用|能帮上)"
            r"|^如有需要[^。！？]{0,20}|^随时(?:欢迎|找我|问我)[^。！？]{0,12}"
            r"|^祝你(?:一切)?顺利|^如果还有问题[^。！？]{0,20}|^以上就是[^。！？]{0,20}"
            r"|(?:欢迎随时|随时欢迎)[^。！？]{0,12}[。！!？?]$"
        ),
    ),
    (
        "boilerplate_intro",
        re.compile(
            r"^(?:随着[^。]{2,20}(?:的)?(?:发展|普及|深入|加快)|在当今(?:社会)?|"
            r"近年来?，?[^。]{0,10}(?:越来越|越来越多)|你有没有(?:没)?有(?:发现|注意到)"
            r"|今天(?:想)?(?:和?大家)?聊聊|先说结论[：:]|事情(?:是)?这样的[：:]|"
            r"不难发现|众所周知|值得一提)"
        ),
    ),
    (
        "boilerplate_outro",
        re.compile(r"^(?:总的?来说|综上所述|总而言之|未来可期|让我们期待|由此可见)[，,：:]"),
    ),
    # 免责/声明式整句（"作为AI"由 role_break 负责，这里管推诿模板）
    (
        "hedging",
        re.compile(r"^(?:我?(?:还)?需要(?:提醒|说明|声明|强调)[，,]|温馨提示[：:]|需要注意的是)"),
    ),
    # 破折号抒情（中文 IM 里几乎不会出现，是典型 AI 排版）
    ("em_dash", re.compile(r"——|—")),
    # 连续感叹号
    ("multi_exclaim", re.compile(r"[!！]{2,}")),
    # markdown 残留：IM 不渲染
    ("markdown", re.compile(r"\*\*|__|#{2,}|^\s*[-*]\s+\S|\|\s*-{3,}")),
]

# 套话前缀：整句里只有开头是助手腔、后面还有真内容时，**只剥前缀不删整句**。
# 直接删整句会把"当然可以，我陪你聊"里的"我陪你聊"一起弄丢，那是更严重的错。
_FILLER_PREFIX_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^(?:好的[，,]?)?(?:当然可以|当然没问题|当然啦|没问题|好嘞)[，,]?\s*"),
    re.compile(
        r"^(?:这是(?:一个)?(?:很好的|好的|好)?问题|你说得(?:很)?对|你观察得(?:很)?敏锐"
        r"|你(?:说|问)得(?:很)?好)[，,：:]\s*"
    ),
]

# 清单标记前缀：IM 里的"1. / (1) / 首先，"是最强的机器排版特征。
# 只剥序号、保留内容——删整句会把角色真正想说的要点一起弄丢。
_LIST_PREFIX_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^\s*(?:\d+[.)、]|[（(]\d+[)）]|[①-⑩])\s*"),
    re.compile(r"^\s*(?:首先|其次|再次|再者|第一|第二|第三)[，,、:：]\s*"),
]

# —— review：书面连接词与空转句，只计数不删（删了可能改坏语义） ——
_REVIEW_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "formal_connective",
        re.compile(
            r"^(?:此外|与此同时|进一步(?:来说)?|更重要的是|值得注意(?:的是)?|"
            r"因此|然而|综上|鉴于|基于此|与此同时)"
        ),
    ),
    (
        "fake_contrast",
        re.compile(r"不是[^。！？]{1,40}[，,]?(?:而是|乃是)[^。！？]{1,40}[。]"),
    ),
    ("importance_claim", re.compile(r"^(?:这(?:也)?(?:说明|意味着|表明)了?|由此可见)")),
    (
        "triple_light",
        re.compile(r"^[^，。！？]{2,12}[，,][^，。！？]{2,12}[，,][^，。！？]{2,12}[。]$"),
    ),
]

# 高频 AI 用词（命中不删，只计数：词本身可能是角色真的要说）
AI_VOCAB: tuple[str, ...] = (
    "赋能", "抓手", "闭环", "全链路", "底层逻辑", "方法论", "生态", "矩阵",
    "赛道", "破局", "解法", "重塑", "颠覆", "引领", "助力", "加持",
    "沉淀", "洞察", "深耕", "构建", "打造", "布局", "焕新", "革新",
    "一站式", "全方位", "多维度", "系统性", "前所未有", "尤为重要",
    "至关重要",
)


@dataclass(frozen=True)
class ScanResult:
    """扫描结果。"""

    kept: str  # 剔除 blocking 句后的文本
    blocking: list[str]  # 命中的 blocking 规则名
    review: list[str]  # 命中的 review 规则名
    vocab: list[str]  # 命中的高频 AI 词


def scan(text: str, *, keep_if_all_blocked: bool = True) -> ScanResult:
    """扫描一段回复文本，返回清洗结果与命中明细。

    只对"整句即套话"的 blocking 命中做剔除。

    ``keep_if_all_blocked`` 决定整段都是套话时的行为：
    - True（默认，供非流式链路与评测使用）：回退原文，绝不产出空回复；
    - False（流式闸门用）：如实返回空串——由调用方决定整轮兜底文案。
      逐句清洗时**不能**每句都回退原文，否则"先说结论：你别急"这种单句套话
      会因为它是段内唯一句子而照样显示出去。
    """
    if not text:
        return ScanResult(kept=text, blocking=[], review=[], vocab=[])
    blocking: list[str] = []
    review: list[str] = []
    kept_parts: list[str] = []
    for sentence in _split_sentences(text):
        # 先剥"当然可以，"这类前缀套话：后面还有真内容时不能连内容一起丢
        stripped = sentence
        for pat in _FILLER_PREFIX_PATTERNS:
            candidate = pat.sub("", stripped)
            if candidate != stripped:
                stripped = candidate.lstrip()
                blocking.append("assistant_opener")
                break
        # 再剥清单序号：去掉"1."这类标记但留住内容，同时计入 blocking 供评测数缺陷
        for pat in _LIST_PREFIX_PATTERNS:
            candidate = pat.sub("", stripped)
            if candidate != stripped:
                stripped = candidate.lstrip()
                blocking.append("list_marker")
                break
        if not (_TEXTUAL_RE.search(stripped) or _EMOJI_RE.search(stripped)):
            continue
        unquoted = _QUOTED_RE.sub("", stripped)
        hit = [name for name, pat in _BLOCKING_PATTERNS if pat.search(unquoted)]
        if hit:
            blocking.extend(hit)
            continue
        review.extend(
            name for name, pat in _REVIEW_PATTERNS if pat.search(unquoted)
        )
        kept_parts.append(stripped)
    kept = "".join(kept_parts).strip()
    if not kept and keep_if_all_blocked:
        kept = text
    return ScanResult(
        kept=kept,
        blocking=blocking,
        review=review,
        vocab=[w for w in AI_VOCAB if w in text],
    )


def has_blocking(text: str) -> bool:
    """是否存在 blocking 级痕迹（评测门禁用）。"""
    return bool(scan(text).blocking)
