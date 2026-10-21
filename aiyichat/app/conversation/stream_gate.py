"""流式回复的"按句缓冲放行"闸门。

要解决的问题：C端网页走 SSE 流式，改动前是 LLM 出一个 token 就原样转发一个，
而所有拟人清洗（动作神态标注 ``（微笑）``、``[IMG:…]``、``[表情:…]``、自称 AI、
角色禁用词、书面腔套话）都排在**全文结束之后**才执行——等于用户全都看得到，
只有落库那份是干净的。本闸门把清洗前移到放行之前：攒到一个句子边界且确认
没有跨 token 的半截标记，才把清洗过的文本发出去。

代价是首字延迟增加约 0.3~1 秒，这是产品侧已确认接受的取舍（宁可慢一点，
也不能让 ``[表情:一个抱抱]`` 出现在屏幕上）。

纯同步、无 IO、不依赖 settings：便于对任意 token 切片顺序做参数化单测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.conversation import ai_tells
from app.conversation.consistency_checker import ConsistencyChecker
from app.conversation.response_processor import ResponseProcessor

# 句子边界：中文句末标点与换行。`|` 单独处理（分隔符是三条竖线）
_SENTENCE_END_RE = re.compile(r"[。！？!?…\n]")
# 消息分隔符
_DELIMITER = "|||"
# 可能成对出现、且内容会被清洗掉的开标
_OPEN_MARKERS = ("[", "（", "(")
# 未闭合标记被强制放行时，从该位置起整段丢弃（半截标记发出去只会更露标）
_DANGLING_RE = re.compile(r"\[[^\]]*$|[（(][^）)]*$|\*[^*]*$")


@dataclass(frozen=True)
class StreamEvent:
    """流式下发事件。

    只在开启放行闸门时使用；未开启时引擎继续 yield 裸 str，SSE 输出与改动前逐字节一致，
    老前端与既有测试契约都不会被波及。

    kind:
    - ``msg``：一条新气泡，``text`` 为整条气泡文本（已清洗）；节奏由服务端在 yield 之前
      sleep 掉，因此不往协议里塞 delay_ms——前端收到时那条气泡就已经"到点了"；
    - ``reset``：收回本轮已显示的全部气泡（复读重跑时用），text 为空。
    """

    kind: str
    text: str = ""


@dataclass
class GateStats:
    """闸门累计计数，供日志与评测观察。"""

    released_segments: int = 0
    dropped_segments: int = 0
    action_tags_removed: int = 0
    role_break_removed: int = 0
    forbidden_removed: int = 0
    ai_tells_removed: int = 0
    forced_release: int = 0
    blocking_hits: list[str] = field(default_factory=list)
    review_hits: list[str] = field(default_factory=list)


class SentenceGate:
    """把 token 流切成"已确认可以示人"的段。

    用法::

        gate = SentenceGate(forbidden_words=[...])
        async for delta in stream:
            for seg in gate.feed(delta):
                yield seg
        for seg in gate.flush():
            yield seg
    """

    def __init__(
        self,
        forbidden_words: list[str] | None = None,
        *,
        max_pending: int = 40,
        processor: ResponseProcessor | None = None,
        checker: ConsistencyChecker | None = None,
    ) -> None:
        self._forbidden = forbidden_words or []
        self._max_pending = max(8, max_pending)
        self._processor = processor or ResponseProcessor()
        self._checker = checker or ConsistencyChecker()
        self._pending = ""
        self._released: list[str] = []
        self.stats = GateStats()

    @property
    def released_text(self) -> str:
        """已放行的全部文本（换行拼接），落库以它为准。"""
        return "\n".join(self._released)

    def feed(self, delta: str) -> list[str]:
        """喂入一个 token 增量，返回本次可以放行并已清洗好的段。"""
        if not delta:
            return []
        self._pending += delta
        out: list[str] = []
        while True:
            segment, keep = self._take_ready_segment()
            if segment is None:
                break
            self._pending = keep
            cleaned = self._clean(segment)
            if cleaned:
                out.append(cleaned)
        return out

    def flush(self) -> list[str]:
        """流结束：放掉最后不足一句的残留；未闭合的半截标记直接丢弃。"""
        out: list[str] = []
        rest = self._pending
        self._pending = ""
        if not rest:
            return out
        cleaned = self._clean(rest)
        if cleaned:
            out.append(cleaned)
        return out

    # —— 内部实现 ——————————————————————————————————————————

    def _take_ready_segment(self) -> tuple[str | None, str]:
        """从 pending 头部取出一个"可放行"的段。

        返回 ``(段, 剩余)``；段为 None 表示还需要继续缓冲。
        """
        text = self._pending
        match = _SENTENCE_END_RE.search(text)
        delim_at = text.find(_DELIMITER)
        # 分隔符与句末谁先到就用谁收口；分隔符本身绝不发出去（网页端会当成正文显示）
        if delim_at >= 0 and (match is None or delim_at <= match.start()):
            if delim_at == 0:
                # 上一段刚在分隔符处收口，这里只吃掉分隔符
                return None, text[len(_DELIMITER) :]
            return text[:delim_at].strip(), text[delim_at + len(_DELIMITER) :]
        cut = -1
        if match is not None:
            cut = match.end()
        if cut < 0:
            # 没有边界：超长强制放行，否则继续攒
            if len(text) < self._max_pending:
                return None, text
            soft = max(
                text.rfind("，"), text.rfind("、"), text.rfind(" "), text.rfind(";"),
                text.rfind("；"),
            )
            if soft <= 0 or soft < len(text) * 0.4:
                self.stats.forced_release += 1
                cut = self._max_pending
            else:
                cut = soft + 1
        segment, keep = text[:cut], text[cut:]
        # 标记未闭合 → 继续缓冲，等闭合或超限再处理
        if self._has_unclosed_marker(segment) and len(text) < self._max_pending * 3:
            return None, text
        return segment, keep

    @staticmethod
    def _has_unclosed_marker(segment: str) -> bool:
        """段内是否存在未闭合的开标（``[IMG:`` 可能被 token 切成两半到达）。"""
        if any(m in segment for m in _OPEN_MARKERS) and not all(
            segment.count(op) <= segment.count(cl)
            for op, cl in (("[", "]"), ("（", "）"), ("(", ")"))
        ):
            return True
        # 星号标注 *轻笑* 成对判定
        return segment.count("*") % 2 == 1

    def _clean(self, segment: str) -> str:
        """对一段文本执行全部清洗规则，顺序不可随意调整。

        1. 动作/神态标签清洗与 ``[IMG]``/``[表情]`` 提取删除（ResponseProcessor）
        2. 人设一致性：自称 AI 与角色禁用词按句剔除（ConsistencyChecker）
        3. 书面腔套话整句剔除（ai_tells）
        清洗全空时保留原文的空白归一结果，避免整条回复凭空消失。
        """
        raw = segment.strip()
        if not raw:
            return ""
        processed, images, stickers = self._processor.process(raw)
        if images or stickers:
            # 网页端没有图片/表情气泡能力，只能把标记删掉；计数留痕便于观察模型在发什么
            self.stats.action_tags_removed += len(images) + len(stickers)
        cleaned = processed
        if cleaned:
            ok, issues = self._checker.check(cleaned, self._forbidden)
            if not ok:
                if any(i.startswith("forbidden:") for i in issues):
                    scrubbed = self._checker.scrub_forbidden(cleaned, self._forbidden)
                    if scrubbed != cleaned:
                        self.stats.forbidden_removed += 1
                    cleaned = scrubbed
                if "role_break" in issues:
                    scrubbed = self._checker.scrub(cleaned)
                    if scrubbed != cleaned:
                        self.stats.role_break_removed += 1
                    cleaned = scrubbed
                # scrub 的"全删则回退原文"护栏是为整条回复设计的，但在这里会让
                # 整段都是越界句的情况照样显示出去。流式路径必须复检：仍不过就整段丢弃，
                # 由引擎在"整轮什么都没放行"时兜底一句安全话术。
                still_ok, remain_issues = self._checker.check(cleaned, self._forbidden)
                if not still_ok:
                    # scrub 判定"全段都违规、删完就没内容"时返回原文，两把尺子对上才是真丢了
                    if "role_break" in remain_issues:
                        self.stats.role_break_removed += 1
                    if any(i.startswith("forbidden:") for i in remain_issues):
                        self.stats.forbidden_removed += 1
                    self.stats.dropped_segments += 1
                    return ""
        scan = ai_tells.scan(cleaned, keep_if_all_blocked=False)
        if scan.blocking:
            self.stats.ai_tells_removed += len(scan.blocking)
            self.stats.blocking_hits.extend(scan.blocking)
        self.stats.review_hits.extend(scan.review)
        cleaned = scan.kept
        cleaned = _DANGLING_RE.sub("", cleaned).strip()
        if not cleaned:
            return ""
        self.stats.released_segments += 1
        self._released.append(cleaned)
        return cleaned
