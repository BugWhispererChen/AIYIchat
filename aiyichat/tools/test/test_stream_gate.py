"""stream_gate 按句缓冲放行闸门单元测试。

最要紧的一类用例是**同一文本按不同 token 切片顺序喂入**：网络分片会把
``[IMG:`` 劈成 ``[I`` + ``MG:``，把 ``（微笑）`` 劈成 ``（微`` + ``笑）``。
无论怎么切，屏幕上都不许出现半截标记或露标文本。
"""

import itertools
from collections.abc import Iterator

import pytest

from app.conversation.stream_gate import SentenceGate

FORBIDDEN = ["老铁", "卧槽"]


def feed_by(gate: SentenceGate, pieces: list[str]) -> list[str]:
    """按给定切片喂入，返回放行段序列。"""
    out: list[str] = []
    for piece in pieces:
        out.extend(gate.feed(piece))
    out.extend(gate.flush())
    return [s for s in out if s]


def sliced(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def permutations_under_test(text: str) -> Iterator[list[str]]:
    """几种代表性切片：整段、逐字、每 2 字、每 3 字、前后两半。"""
    yield [text]
    yield list(text)
    yield sliced(text, 2)
    yield sliced(text, 3)
    yield [text[: len(text) // 2], text[len(text) // 2 :]]


class TestNeverLeaksMarkers:
    """任何切片方式下都不得露出内部标记。"""

    @pytest.mark.parametrize(
        "text",
        [
            "今天辛苦啦（微笑）早点休息哦。",
            "给你画一张[IMG:窗边的猫]看看呀。",
            "抱抱你[表情:一个拥抱]别难过。",
            "嗯嗯|||我在的|||今天怎么这么晚",
            "先说结论：你别急。当然可以，我陪你聊。",
        ],
    )
    def test_no_raw_marker_survives_any_slicing(self, text: str) -> None:
        for pieces in permutations_under_test(text):
            gate = SentenceGate(FORBIDDEN)
            released = feed_by(gate, pieces)
            joined = "".join(released)
            assert "IMG" not in joined, pieces
            assert "[表情" not in joined, pieces
            assert "（微笑）" not in joined, pieces
            assert "|||" not in joined, pieces
            assert "先说结论" not in joined, pieces
            assert "当然可以" not in joined, pieces

    def test_half_open_tag_at_end_of_stream_is_dropped(self) -> None:
        gate = SentenceGate(FORBIDDEN)
        released = feed_by(gate, ["好累啊。[IM"])
        assert "".join(released).count("[") == 0

    def test_unclosed_bracket_never_emits_partial(self) -> None:
        gate = SentenceGate(FORBIDDEN)
        released = feed_by(gate, ["你看这只猫", "[IMG:", "趴在窗台", "上很可爱"])
        joined = "".join(released)
        assert "IMG" not in joined
        assert "猫" in joined or "窗台" in joined


class TestRoleBreakAndForbidden:
    """自曝 AI 与禁用词在放行前就被剔掉。"""

    def test_role_break_sentence_not_released(self) -> None:
        gate = SentenceGate(FORBIDDEN)
        released = feed_by(
            gate, ["我是一个AI助手。不过我懂你的累，先去吃点东西吧。"]
        )
        joined = "".join(released)
        assert "AI" not in joined
        assert "先去吃点东西" in joined

    def test_forbidden_word_sentence_not_released(self) -> None:
        gate = SentenceGate(FORBIDDEN)
        released = feed_by(gate, ["老铁你今天咋样。累的话就早点睡。"])
        joined = "".join(released)
        assert "老铁" not in joined
        assert "早点睡" in joined

    def test_slice_by_char_still_blocks_role_break(self) -> None:
        for pieces in permutations_under_test(
            "作为语言模型我给不了建议。但我在听，你说。"
        ):
            joined = "".join(feed_by(SentenceGate(FORBIDDEN), pieces))
            assert "语言模型" not in joined


class TestReleaseShape:
    """放行段的结构。"""

    def test_delimiter_creates_separate_segments(self) -> None:
        segs = feed_by(SentenceGate(FORBIDDEN), ["在吗|||今天累不累|||早点休息"])
        assert len(segs) >= 2
        assert all("|||" not in s for s in segs)

    def test_sentence_ends_split_segments(self) -> None:
        segs = feed_by(SentenceGate(FORBIDDEN), ["第一句。第二句！第三句？"])
        assert len(segs) == 3

    def test_long_sentence_without_boundary_force_released(self) -> None:
        gate = SentenceGate(FORBIDDEN, max_pending=20)
        segs = feed_by(gate, ["没有标点的一长串中文" * 6])
        assert segs
        assert gate.stats.forced_release >= 1
        assert sum(len(s) for s in segs) >= 20

    def test_empty_and_noop_inputs(self) -> None:
        gate = SentenceGate(FORBIDDEN)
        assert gate.feed("") == []
        assert gate.flush() == []
        assert gate.released_text == ""

    def test_clean_text_released_verbatim(self) -> None:
        segs = feed_by(SentenceGate(FORBIDDEN), ["今晚月色真美，适合早点睡。"])
        assert segs == ["今晚月色真美，适合早点睡。"]


class TestReleasedEqualsStored:
    """所见即所存：闸门产物即落库文本。"""

    def test_released_text_matches_segments(self) -> None:
        gate = SentenceGate(FORBIDDEN)
        segs = feed_by(gate, ["辛苦啦。[表情:抱抱]明天还要早起呢。"])
        assert gate.released_text == "\n".join(segs)
        assert gate.stats.released_segments == len(segs)

    def test_stats_record_removals(self) -> None:
        gate = SentenceGate(FORBIDDEN)
        feed_by(gate, ["当然可以。我作为AI陪你聊聊（微笑）。老铁加油。"])
        assert gate.stats.ai_tells_removed >= 1
        assert gate.stats.role_break_removed >= 1
        assert gate.stats.forbidden_removed >= 1


class TestForbiddenOptional:
    """不配禁用词时闸门仍工作。"""

    def test_no_forbidden_words(self) -> None:
        segs = feed_by(SentenceGate(None), ["今天也辛苦啦。早点休息。"])
        assert segs == ["今天也辛苦啦。", "早点休息。"]

    def test_empty_forbidden_list(self) -> None:
        segs = feed_by(SentenceGate([]), ["在的呢。"])
        assert segs == ["在的呢。"]


def test_all_slicings_agree_on_content_not_position() -> None:
    """不同切片得到的文本集合应一致（切分位置会变，内容不能变）。"""
    text = "辛苦啦。[表情:抱抱]明天再聊，早点休息哦。"
    variants = {
        "".join(feed_by(SentenceGate(FORBIDDEN), pieces))
        for pieces in itertools.islice(permutations_under_test(text), 5)
    }
    assert len(variants) == 1, variants
