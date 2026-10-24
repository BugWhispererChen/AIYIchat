"""LLM 回复后处理器。

负责清理动作标签、提取图片生成指令与表情包指令、规范化空白字符。
支持将常见表情描述词转换为 Unicode emoji 以增强情感表达。
"""

import re
from collections.abc import AsyncIterator

# 常见表情描述 → Unicode emoji 映射表
EMOTION_EMOJI_MAP: dict[str, str] = {
    "笑": "😊", "微笑": "😊", "开心": "😄", "高兴": "😄",
    "哈哈": "😂", "大笑": "🤣", "害羞": "😳", "脸红": "😳",
    "难过": "😢", "伤心": "😢", "哭": "😭", "大哭": "😭",
    "生气": "😠", "愤怒": "😡", "惊讶": "😮", "震惊": "😱",
    "思考": "🤔", "想": "🤔", "尴尬": "😅", "苦笑": "😅",
    "得意": "😏", "撇嘴": "😒", "无奈": "😑", "无语": "😑",
    "紧张": "😰", "焦虑": "😰", "叹气": "😮\u200d💨", "叹息": "😮\u200d💨",
    "眨眼": "😉", "委屈": "🥺", "可怜": "🥺",
    "拥抱": "🤗", "抱抱": "🤗",
    "亲亲": "😘", "飞吻": "😘", "么么哒": "😘",
    "调皮": "😜", "吐舌": "😜",
    "比心": "❤️", "爱心": "❤️", "心": "❤️",
    "点头": "👍", "赞": "👍", "棒": "👍",
    "摇头": "🙅", "不行": "🙅",
    "鼓掌": "👏", "加油": "💪", "捂脸": "🤦",
    "偷笑": "🤭", "嘻嘻": "🤭", "嘿嘿": "🤭",
    "困": "😴", "睡": "😴", "打哈欠": "🥱",
    "疑惑": "😕", "困惑": "😕",
    "呆": "😐", "发呆": "😐",
    "感动": "🥹", "温暖": "🥰", "甜蜜": "🥰",
    "期待": "🤩", "星星眼": "🤩",
    "撒娇": "🥺", "嘟嘴": "😗",
    "吃惊": "😲", "目瞪口呆": "😲",
    "恶心": "🤢", "呕": "🤢",
    "冷": "🥶", "热": "🥵",
    "闭嘴": "🤐", "嘘": "🤫",
}


class ResponseProcessor:
    """处理 LLM 原始输出，生成干净的回复文本和元数据。"""

    # 匹配括号/星号中的动作、表情、神态描述
    _ACTION_TAG_RE = re.compile(
        r'[（(][^）)]{1,30}[）)]'   # 中英文括号内短内容
        r'|\*[^*]{1,30}\*'          # 星号标注
    )

    # 匹配 [IMG:description] / [IMG:self:description] / [生成图片:description] 标签
    # 分组 1：可选的 self 标记（本人出镜）；分组 2：图片描述
    _IMG_TAG_RE = re.compile(
        r'\[(?:IMG|生成图片)\s*[:：]\s*(?:(self)\s*[:：]\s*)?([^\]]+)\]',
        re.IGNORECASE,
    )

    # 匹配 [表情:语义描述] / [STICKER:语义描述] 表情包标签，分组 1：描述文本。
    # 与 QQ 收侧解析出的 [表情:描述] 格式对称，仅从 LLM 输出中提取。
    _STICKER_TAG_RE = re.compile(
        r'\[(?:表情|STICKER)\s*[:：]\s*([^\]]+)\]',
        re.IGNORECASE,
    )

    _TIME_TAG_RE = re.compile(r'^\s*\[(?:\d{4}-)?\d{2}-\d{2} \d{2}:\d{2}\]\s*', re.M)

    @staticmethod
    def strip_time_tags(text: str) -> str:
        return ResponseProcessor._TIME_TAG_RE.sub('', text)

    @staticmethod
    async def strip_time_tags_stream(chunks: AsyncIterator[str]) -> AsyncIterator[str]:
        # 行首标签可能跨多个增量，确认不是时间标签前不能下发。
        templates = ("[dd-dd dd:dd]", "[dddd-dd-dd dd:dd]")
        pending = ""
        at_line_start = True
        skipping_tag_space = False
        try:
            async for chunk in chunks:
                output: list[str] = []
                for char in chunk:
                    if not at_line_start:
                        output.append(char)
                        at_line_start = char == "\n"
                        continue
                    if skipping_tag_space and char.isspace():
                        continue
                    skipping_tag_space = False
                    pending += char
                    probe = pending.lstrip()
                    if not probe:
                        continue
                    if ResponseProcessor._TIME_TAG_RE.fullmatch(pending):
                        pending = ""
                        skipping_tag_space = True
                        continue
                    if any(
                        len(probe) < len(template) and all(
                            actual.isdecimal() if expected == "d" else actual == expected
                            for actual, expected in zip(probe, template)
                        )
                        for template in templates
                    ):
                        continue
                    output.append(pending)
                    pending = ""
                    at_line_start = char == "\n"
                if output:
                    yield "".join(output)
        except Exception:
            if pending:
                yield ResponseProcessor.strip_time_tags(pending)
            raise
        if pending:
            yield ResponseProcessor.strip_time_tags(pending)

    def process(
        self, text: str
    ) -> tuple[str, list[tuple[str, bool]], list[str]]:
        """
        处理 LLM 原始回复文本。

        Returns:
            (clean_text, image_specs, sticker_specs)：
            - image_specs: (图片描述, 是否本人出镜) 列表
            - sticker_specs: [表情:描述] 标签提取的语义描述列表
        """
        cleaned = self._clean_action_tags(self.strip_time_tags(text))
        image_specs = self._extract_image_tags(cleaned)
        sticker_specs = self._extract_sticker_tags(cleaned)
        clean_text = self._IMG_TAG_RE.sub('', cleaned).strip()
        clean_text = self._STICKER_TAG_RE.sub('', clean_text).strip()
        clean_text = re.sub(r'\n{3,}', '\n\n', clean_text).strip()
        return clean_text, image_specs, sticker_specs

    @staticmethod
    def _extract_inner(tag_text: str) -> str:
        """提取括号或星号标签内部的纯文字。"""
        return re.sub(r'[（()）*]', '', tag_text).strip()

    def _clean_action_tags(self, text: str) -> str:
        """将 LLM 回复中的表情标签转为 emoji，动作/状态标签仍删除。"""

        def _replace_tag(match: re.Match[str]) -> str:
            inner = self._extract_inner(match.group(0))
            emoji = EMOTION_EMOJI_MAP.get(inner)
            # 有映射的表情词转为 emoji，其余（动作/神态描述）删除
            return emoji if emoji else ""

        cleaned = self._ACTION_TAG_RE.sub(_replace_tag, text)
        cleaned = re.sub(r'[ \t]+', ' ', cleaned).strip()
        cleaned = re.sub(r'([，。！？、])\s*\1+', r'\1', cleaned)
        return cleaned

    def clean_display(self, text: str) -> str:
        """只去掉不该出现在聊天气泡里的标签，不做空白归一与首尾 strip。

        与 `process()` 的差别是为"边流边发"准备的：`process()` 末尾会压空行并 strip，
        逐段调用会把进行中的空格与换行吃掉，所以下发路径用这个更轻的清洗。
        """

        def _replace_tag(match: re.Match[str]) -> str:
            inner = self._extract_inner(match.group(0))
            return EMOTION_EMOJI_MAP.get(inner, "")

        cleaned = self._ACTION_TAG_RE.sub(_replace_tag, text)
        cleaned = self._IMG_TAG_RE.sub("", cleaned)
        cleaned = self._STICKER_TAG_RE.sub("", cleaned)
        return cleaned


    def _extract_image_tags(self, text: str) -> list[tuple[str, bool]]:
        """从 LLM 回复中提取 [IMG:xxx] / [IMG:self:xxx] 描述列表。"""
        return [
            (desc.strip(), bool(self_flag))
            for self_flag, desc in self._IMG_TAG_RE.findall(text)
            if desc.strip()
        ]

    def _extract_sticker_tags(self, text: str) -> list[str]:
        """从 LLM 回复中提取 [表情:语义描述] 标签的描述列表。"""
        return [
            desc.strip()
            for desc in self._STICKER_TAG_RE.findall(text)
            if desc.strip()
        ]

_MAX_DISPLAY_HOLD = 200


class DisplayStreamCleaner:
    """把增量文本切成"可以安全下发的前缀"，避免半截标签先显示出来。

    标签可能横跨多个增量（`[IM` 然后 `G:sunset]`），逐块原样下发就等于把内部标记
    推给浏览器；而整段等齐再发又毁掉流式体验。这里只扣住"最后一个尚未闭合的开括号
    之后的部分"，其余照常下发。
    """

    _OPENERS = ("[", "（", "(" , "*")
    _CLOSERS = ("]", "）", ")", "*")

    def __init__(self, processor: ResponseProcessor) -> None:
        self._processor = processor
        self._pending = ""

    def push(self, delta: str) -> list[str]:
        self._pending += delta
        cut = self._safe_boundary(self._pending)
        # 扣住量必须有上限：模型如果吐出一个永远不闭合的 `[`，无上限就等于整条回复
        # 都不下发（屏幕空白），比原先"标签露出来"更糟。超过上限就照常下发，
        # 最坏情况退化成改动前的表现。200 足够容纳一条完整的 [IMG:…] 描述。
        if len(self._pending) - cut > _MAX_DISPLAY_HOLD:
            cut = len(self._pending) - _MAX_DISPLAY_HOLD
        if cut <= 0:
            return []
        emit = self._pending[:cut]
        self._pending = self._pending[cut:]
        text = self._processor.clean_display(emit)
        return [text] if text else []

    def flush(self) -> list[str]:
        """收尾：把最后一段（含仍未闭合的开括号）发出去。"""
        if not self._pending:
            return []
        emit = self._pending
        self._pending = ""
        text = self._processor.clean_display(emit)
        return [text] if text else []

    @classmethod
    def _safe_boundary(cls, text: str) -> int:
        """从右往左找第一个"后面没有闭括号"的开括号，它之后的内容还不能下发。

        `*` 自身既是开也是闭，右扫会被它自己满足，所以另按奇偶数判定：
        落单的 `*` 说明动作标签还没写完，同样扣住。
        """
        boundary = len(text)
        closer_seen = False
        for index in range(len(text) - 1, -1, -1):
            char = text[index]
            if char in cls._CLOSERS and char != "*":
                closer_seen = True
            elif char in cls._OPENERS and not closer_seen:
                boundary = min(boundary, index)
                break
        stars = text.count("*")
        if stars % 2 == 1:
            boundary = min(boundary, text.rfind("*"))
        return boundary

