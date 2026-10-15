"""ResponseProcessor 单元测试。

覆盖 engine 接入 ResponseProcessor 后的清洗行为：
- 表情描述词按 EMOTION_EMOJI_MAP 转 emoji
- 无映射的动作/神态描述被删除
- [IMG:xxx] / [IMG:self:xxx] 标签被提取并从正文移除
"""

import asyncio
from collections.abc import AsyncIterator

import pytest

from app.conversation.response_processor import ResponseProcessor


class TestResponseProcessor:
    """ResponseProcessor.process 功能测试集。"""

    def setup_method(self) -> None:
        self.processor = ResponseProcessor()

    def test_emotion_tag_converted_to_emoji(self) -> None:
        """括号内的表情词应转换为对应 emoji。"""
        clean_text, images, stickers = self.processor.process("你好呀（微笑）今天怎么样")
        assert "😊" in clean_text
        assert "微笑" not in clean_text
        assert images == []
        assert stickers == []

    def test_action_tag_removed(self) -> None:
        """无 emoji 映射的动作描写应被删除。"""
        clean_text, _, _ = self.processor.process("（缓缓起身走向窗边）外面下雨了")
        assert "缓缓起身" not in clean_text
        assert "外面下雨了" in clean_text

    def test_star_emotion_tag_converted(self) -> None:
        """星号标注的表情词同样转换为 emoji。"""
        clean_text, _, _ = self.processor.process("*开心* 我们出去玩吧")
        assert "😄" in clean_text
        assert "开心" not in clean_text

    def test_image_tag_extracted_and_stripped(self) -> None:
        """[IMG:xxx] 标签应被提取到列表并从正文移除。"""
        clean_text, images, _ = self.processor.process(
            "给你看看风景[IMG:海边日落的照片]很美吧"
        )
        assert images == [("海边日落的照片", False)]
        assert "[IMG:" not in clean_text
        assert "海边日落的照片" not in clean_text

    def test_image_self_tag_extracted(self) -> None:
        """[IMG:self:xxx] 标签应标记为本人出镜并从正文移除。"""
        clean_text, images, _ = self.processor.process(
            "刚拍的[IMG:self:realistic photo, selfie in a cafe]好看吗"
        )
        assert images == [("realistic photo, selfie in a cafe", True)]
        assert "[IMG:" not in clean_text
        assert "selfie" not in clean_text

    def test_image_self_tag_case_insensitive(self) -> None:
        """self 标记大小写不敏感，中文冒号也可识别。"""
        _, images, _ = self.processor.process("[IMG：SELF：a selfie photo]")
        assert images == [("a selfie photo", True)]

    def test_plain_text_unchanged(self) -> None:
        """无标签的纯文本应保持内容不变。"""
        clean_text, images, stickers = self.processor.process("今天天气真好")
        assert clean_text == "今天天气真好"
        assert images == []
        assert stickers == []

    @pytest.mark.parametrize("text,expected", [
        ("[09-13 02:24] 你好", "你好"),
        (" \n[2026-09-13 02:24] 你好", "你好"),
        ("你好\n[09-13 02:25] 晚安", "你好\n晚安"),
        ("正文 [09-13 02:24] 是引用", "正文 [09-13 02:24] 是引用"),
        ("[表情:晚安]", "[表情:晚安]"),
        ("[IMG:海边日落]", "[IMG:海边日落]"),
        ("[09-13 02:", "[09-13 02:"),
        ("短句", "短句"),
        ("", ""),
    ])
    def test_strip_time_tags(self, text: str, expected: str) -> None:
        assert self.processor.strip_time_tags(text) == expected

    def test_process_removes_time_before_other_tags(self) -> None:
        clean, images, stickers = self.processor.process(
            "[2026-09-13 02:24] 晚安[表情:晚安][IMG:月亮]"
        )
        assert clean == "晚安"
        assert images == [("月亮", False)]
        assert stickers == ["晚安"]

    @pytest.mark.parametrize("text", [
        "[09-13 02:24] 你好", "\n [2026-09-13 02:24] \n你好", "你好",
        "第一行\n[09-13 02:25] 第二行", "[IMG:月亮]", "[表情:晚安]",
        "[09-13 02:", "", " 正常缩进", "正文 [09-13 02:24] 引用",
    ])
    def test_stream_all_split_positions(self, text: str) -> None:
        async def collect(parts: list[str]) -> str:
            async def source() -> AsyncIterator[str]:
                for part in parts:
                    yield part
            return "".join([part async for part in self.processor.strip_time_tags_stream(source())])

        for split in range(len(text) + 1):
            assert asyncio.run(collect([text[:split], text[split:]])) == self.processor.strip_time_tags(text)
        assert asyncio.run(collect(list(text))) == self.processor.strip_time_tags(text)

    def test_stream_flushes_short_reply_on_error(self) -> None:
        async def collect() -> list[str]:
            async def source() -> AsyncIterator[str]:
                yield "[09-"
                raise RuntimeError("模拟流中断")
            output: list[str] = []
            with pytest.raises(RuntimeError):
                async for part in self.processor.strip_time_tags_stream(source()):
                    output.append(part)
            return output

        assert asyncio.run(collect()) == ["[09-"]
