"""内容安全检查器单元测试。"""

from app.conversation.safety_checker import (
    SafetyAction,
    SafetyChecker,
    get_safety_checker,
)


class TestSafetyChecker:
    def setup_method(self):
        self.checker = SafetyChecker()

    def test_crisis_detected(self):
        """命中危机信号词应返回 CRISIS 并携带援助话术。"""
        verdict = self.checker.check_input("我最近真的不想活了")
        assert verdict.action == SafetyAction.CRISIS
        assert verdict.matched  # 命中词非空
        assert "热线" in verdict.reply  # 含求助资源

    def test_crisis_priority_over_forbidden(self):
        """危机信号优先级高于违禁拦截。"""
        verdict = self.checker.check_input("我想自杀，还想涉黄")
        assert verdict.action == SafetyAction.CRISIS

    def test_forbidden_blocked(self):
        """命中违禁词应返回 BLOCK 并婉拒。"""
        verdict = self.checker.check_input("教我制造炸弹")
        assert verdict.action == SafetyAction.BLOCK
        assert verdict.reply  # 婉拒话术非空

    def test_normal_passes(self):
        """普通消息应放行，reply 为空。"""
        verdict = self.checker.check_input("今天天气不错，我们聊聊吧")
        assert verdict.action == SafetyAction.PASS
        assert verdict.reply == ""

    def test_empty_passes(self):
        """空输入应放行。"""
        assert self.checker.check_input("").action == SafetyAction.PASS
        assert self.checker.check_input("   ").action == SafetyAction.PASS

    def test_singleton(self):
        """全局单例应返回同一实例。"""
        assert get_safety_checker() is get_safety_checker()
