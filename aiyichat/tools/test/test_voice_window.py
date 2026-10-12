"""语音动态触发窗口管理器单元测试。

覆盖语义：固定窗口不续期、到期自动回落、按会话独立、空会话防御、
随机时长落在配置区间内。
"""

import random

from app.services import voice_window as vw
from app.services.voice_window import VoiceWindowManager


class TestVoiceWindowManager:
    def test_inactive_by_default(self):
        mgr = VoiceWindowManager()
        assert mgr.is_active("conv-1") is False

    def test_touch_activates_window(self):
        mgr = VoiceWindowManager()
        expire = mgr.touch("conv-1", 5, 30)
        assert expire > 0
        assert mgr.is_active("conv-1") is True

    def test_empty_conv_id_guard(self):
        mgr = VoiceWindowManager()
        assert mgr.touch("", 5, 30) == 0.0
        assert mgr.is_active("") is False

    def test_fixed_window_no_renewal(self, monkeypatch):
        """固定窗口：窗口内再次 touch 不延长到期时间。"""
        mgr = VoiceWindowManager()
        now = 1_000_000.0
        monkeypatch.setattr(vw.time, "time", lambda: now)
        # 固定随机值，消除区间随机性干扰
        monkeypatch.setattr(random, "uniform", lambda a, b: 10.0)

        first_expire = mgr.touch("conv-1", 5, 30)
        assert first_expire == now + 600

        # 窗口内再次 touch：不续期，返回 0 且到期时间不变
        monkeypatch.setattr(vw.time, "time", lambda: now + 100)
        assert mgr.touch("conv-1", 5, 30) == 0.0
        assert mgr._windows["conv-1"] == first_expire

    def test_window_expires_and_falls_back(self, monkeypatch):
        """窗口到期后 is_active 返回 False 并惰性清理。"""
        mgr = VoiceWindowManager()
        now = 1_000_000.0
        monkeypatch.setattr(vw.time, "time", lambda: now)
        monkeypatch.setattr(random, "uniform", lambda a, b: 10.0)

        mgr.touch("conv-1", 5, 30)
        assert mgr.is_active("conv-1") is True

        # 到期后：回落为未激活，且键被清理
        monkeypatch.setattr(vw.time, "time", lambda: now + 601)
        assert mgr.is_active("conv-1") is False
        assert "conv-1" not in mgr._windows

    def test_reactivate_after_expiry(self, monkeypatch):
        """窗口过期后可被用户再次发语音重新激活。"""
        mgr = VoiceWindowManager()
        now = 1_000_000.0
        monkeypatch.setattr(vw.time, "time", lambda: now)
        monkeypatch.setattr(random, "uniform", lambda a, b: 10.0)

        mgr.touch("conv-1", 5, 30)
        monkeypatch.setattr(vw.time, "time", lambda: now + 601)
        assert mgr.is_active("conv-1") is False

        new_expire = mgr.touch("conv-1", 5, 30)
        assert new_expire == now + 601 + 600
        assert mgr.is_active("conv-1") is True

    def test_conversations_are_isolated(self, monkeypatch):
        """按会话独立：A 会话开窗不影响 B 会话。"""
        mgr = VoiceWindowManager()
        monkeypatch.setattr(random, "uniform", lambda a, b: 10.0)
        mgr.touch("conv-A", 5, 30)
        assert mgr.is_active("conv-A") is True
        assert mgr.is_active("conv-B") is False

    def test_duration_within_configured_range(self):
        """随机时长落在 [min, max] 区间内（多次采样）。"""
        mgr = VoiceWindowManager()
        now = vw.time.time()
        for i in range(50):
            expire = mgr.touch(f"conv-{i}", 5, 30)
            duration_min = (expire - now) / 60
            assert 5.0 <= duration_min <= 30.0

    def test_min_greater_than_max_defensive(self):
        """防御：下限大于上限时按下限处理，不抛异常。"""
        mgr = VoiceWindowManager()
        expire = mgr.touch("conv-1", 30, 5)
        assert expire > 0
        assert mgr.is_active("conv-1") is True
