"""人设一致性自检单元测试。

覆盖 ConsistencyChecker：
- 正常回复通过
- 角色越界（自称AI）检出
- 禁用词检出
- scrub 剔除越界句，全部命中时退回原文
"""

from app.conversation.consistency_checker import ConsistencyChecker


class TestConsistencyChecker:
    """人设一致性校验测试集。"""

    def setup_method(self) -> None:
        self.checker = ConsistencyChecker()

    def test_clean_text_passes(self) -> None:
        ok, issues = self.checker.check("今天过得怎么样呀？")
        assert ok
        assert not issues

    def test_role_break_detected(self) -> None:
        ok, issues = self.checker.check("作为一个AI，我无法真正感受情绪")
        assert not ok
        assert "role_break" in issues

    def test_forbidden_word_detected(self) -> None:
        ok, issues = self.checker.check("你这个笨蛋", ["笨蛋"])
        assert not ok
        assert any(i.startswith("forbidden") for i in issues)

    def test_scrub_removes_role_break_sentence(self) -> None:
        text = "我是一个AI助手。不过我很乐意陪你聊天！"
        scrubbed = self.checker.scrub(text)
        assert "AI" not in scrubbed
        assert "陪你聊天" in scrubbed

    def test_scrub_keeps_original_when_all_removed(self) -> None:
        text = "作为AI我不能这样。"
        scrubbed = self.checker.scrub(text)
        # 全部句子被剔除时退回原文，避免空回复
        assert scrubbed == text
