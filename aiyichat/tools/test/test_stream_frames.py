"""流式 SSE 帧编码与前端节奏契约测试。

覆盖两件事：
1. ``stream_frames`` 在闸门关闭时必须产出与改动前**逐字节一致**的帧（老前端不受影响）；
2. ``web-chat.js`` 的关键契约仍以静态断言守住——本项目前端没有浏览器测试环境，
   但"不许 innerHTML""必须认 [MSG]/[RESET]""开关回退时仍能渲染 |||"这几条一旦破了，
   拟人效果和安全都会哑掉。
"""

from pathlib import Path

from app.api.conversations_api import stream_frames
from app.conversation.stream_gate import StreamEvent

WEB_CHAT_JS = Path(__file__).resolve().parents[2] / "app" / "static" / "js" / "web-chat.js"


def _body(frame: str) -> str:
    """从 data 帧里取回 payload（模拟前端 payloadOf 的切片与反解义）。"""
    assert frame.startswith("data: ") and frame.endswith("\n\n")
    return frame[6:-2].replace("\\n", "\n")


class TestLegacyFramesByteEqual:
    """闸门关闭：裸 str 产出必须与改动前逐字节一致。"""

    def test_plain_text_frame(self) -> None:
        assert stream_frames("你好") == ["data: 你好\n\n"]

    def test_newline_escaped(self) -> None:
        assert stream_frames("第一行\n第二行") == ["data: 第一行\\n第二行\n\n"]

    def test_cr_dropped(self) -> None:
        assert stream_frames("a\r\nb") == ["data: a\\nb\n\n"]

    def test_error_token_still_plain(self) -> None:
        assert stream_frames("[ERROR] 炸了") == ["data: [ERROR] 炸了\n\n"]


class TestGatedFrames:
    """闸门开启：StreamEvent 编成气泡边界帧 + 正文帧。"""

    def test_msg_event_two_frames(self) -> None:
        frames = stream_frames(StreamEvent(kind="msg", text="今晚辛苦啦"))
        assert frames == [
            "data: [MSG]\n\n",
            "data: 今晚辛苦啦\n\n",
        ]

    def test_reset_single_frame(self) -> None:
        assert stream_frames(StreamEvent(kind="reset")) == ["data: [RESET]\n\n"]

    def test_empty_msg_produces_nothing(self) -> None:
        assert stream_frames(StreamEvent(kind="msg", text="")) == []

    def test_multiline_body_stays_one_frame(self) -> None:
        frames = stream_frames(StreamEvent(kind="msg", text="一\n二"))
        assert len(frames) == 2
        assert _body(frames[1]) == "一\n二"

    def test_frame_sequence_decodes_to_bubbles(self) -> None:
        """一条回复两气泡：[MSG] 文本 [MSG] 文本，前端据此开两条。"""
        events = [
            StreamEvent(kind="msg", text="在的呀"),
            StreamEvent(kind="msg", text="今天怎么这么晚"),
        ]
        payloads = [_body(f) for e in events for f in stream_frames(e)]
        assert payloads == ["[MSG]", "在的呀", "[MSG]", "今天怎么这么晚"]
        bubbles = []
        for p in payloads:
            if p == "[MSG]":
                bubbles.append("")
            elif bubbles:
                bubbles[-1] += p
        assert bubbles == ["在的呀", "今天怎么这么晚"]


class TestWebChatJsContract:
    """前端脚本静态契约。"""

    def setup_method(self) -> None:
        assert WEB_CHAT_JS.exists(), WEB_CHAT_JS
        self.src = WEB_CHAT_JS.read_text(encoding="utf-8")

    def test_no_html_injection(self) -> None:
        # 回复文本一律走 textContent：模型输出的尖括号标签不许变成 DOM
        assert ".innerHTML" not in self.src
        assert "insertAdjacentHTML" not in self.src

    def test_handles_msg_and_reset_frames(self) -> None:
        assert "'[MSG]'" in self.src
        assert "'[RESET]'" in self.src
        assert "MSG_FRAME" in self.src and "RESET_FRAME" in self.src

    def test_keeps_delimiter_fallback_for_switch_off(self) -> None:
        # 闸门关闭时后端仍下发含 ||| 的裸增量，前端必须还能拆气泡
        assert "MSG_DELIM" in self.src and "'|||'" in self.src
        assert "renderReply" in self.src

    def test_paced_path_appends_not_redraws(self) -> None:
        assert "appendInto" in self.src and "openBubble" in self.src
        assert "resetTurn" in self.src

    def test_reset_turn_clears_extras(self) -> None:
        assert "dropExtras" in self.src

    def test_typing_indicator_still_managed(self) -> None:
        # 占位气泡的 typing 类必须被移除，否则页面永远停在"正在输入"
        assert self.src.count("classList.remove('typing')") >= 3
