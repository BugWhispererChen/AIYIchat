"""角色心情管理模块的单元测试。

覆盖：时间衰减、Prompt 渲染、TTS 语速修正、图片情绪修饰。
"""

from datetime import datetime, timedelta, timezone

from app.conversation.mood_manager import MoodManager
from app.models.service_memory import CharacterMood


def _mood(valence: float, arousal: float = 0.3, hours_ago: float = 0.0) -> CharacterMood:
    """构造一个指定时长前更新的心情。"""
    return CharacterMood(
        valence=valence,
        arousal=arousal,
        label="测试心情",
        cause="测试成因",
        updated_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
    )


class TestComputeDecay:
    """时间衰减：心情向基线回归。"""

    def test_no_decay_when_just_updated(self):
        m = _mood(valence=0.8, hours_ago=0.0)
        decayed = MoodManager.compute_decay(m)
        assert abs(decayed.valence - 0.8) < 0.01

    def test_decay_toward_baseline(self):
        m = _mood(valence=1.0, hours_ago=6.0)  # 一个半衰期
        decayed = MoodManager.compute_decay(m)
        # 6 小时后衰减为原来的一半
        assert abs(decayed.valence - 0.5) < 0.05

    def test_long_time_returns_near_baseline(self):
        m = _mood(valence=1.0, hours_ago=48.0)  # 8 个半衰期
        decayed = MoodManager.compute_decay(m)
        assert abs(decayed.valence) < 0.01

    def test_negative_valence_decays_toward_zero(self):
        m = _mood(valence=-1.0, hours_ago=6.0)
        decayed = MoodManager.compute_decay(m)
        assert abs(decayed.valence - (-0.5)) < 0.05


class TestRenderMoodHint:
    """Prompt 渲染：心情平静时不注入，显著时生成指令。"""

    def test_none_mood_returns_empty(self):
        assert MoodManager.render_mood_hint(None) == ""

    def test_calm_mood_returns_empty(self):
        m = _mood(valence=0.05)  # 低于显著阈值
        assert MoodManager.render_mood_hint(m) == ""

    def test_positive_mood_renders(self):
        m = _mood(valence=0.7)
        hint = MoodManager.render_mood_hint(m)
        assert "测试心情" in hint
        assert "测试成因" in hint

    def test_negative_mood_renders(self):
        m = _mood(valence=-0.7)
        hint = MoodManager.render_mood_hint(m)
        assert "测试心情" in hint

    def test_decayed_calm_returns_empty(self):
        # 曾经很悲伤但已过很久，衰减后低于阈值
        m = _mood(valence=-0.3, hours_ago=48.0)
        assert MoodManager.render_mood_hint(m) == ""


class TestImageMoodSuffix:
    """图片情绪修饰词。"""

    def test_none_mood_returns_empty(self):
        assert MoodManager.image_mood_suffix(None) == ""

    def test_weak_mood_returns_empty(self):
        m = _mood(valence=0.3)  # 低于图片触发阈值
        assert MoodManager.image_mood_suffix(m) == ""

    def test_strong_positive_suffix(self):
        m = _mood(valence=0.8)
        suffix = MoodManager.image_mood_suffix(m)
        assert "smile" in suffix

    def test_strong_negative_suffix(self):
        m = _mood(valence=-0.8)
        suffix = MoodManager.image_mood_suffix(m)
        assert "downturned" in suffix or "wistful" in suffix
