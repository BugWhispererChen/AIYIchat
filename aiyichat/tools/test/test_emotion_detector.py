from app.conversation.emotion_detector import detect_emotion


class TestEmotionDetector:
    def test_detect_sad(self):
        assert detect_emotion("我今天心情不好") == "sad"
        assert detect_emotion("好想哭啊") == "sad"
        assert detect_emotion("太痛苦了，受不了") == "sad"

    def test_detect_happy(self):
        assert detect_emotion("太好了，我通过了！") == "happy"
        assert detect_emotion("好开心啊") == "happy"

    def test_detect_angry(self):
        assert detect_emotion("气死我了！太过分了") == "angry"

    def test_detect_anxious(self):
        assert detect_emotion("压力大，睡不着") == "anxious"

    def test_detect_lonely(self):
        assert detect_emotion("好孤独，没人陪") == "lonely"

    def test_detect_neutral(self):
        assert detect_emotion("今天天气不错") is None
        assert detect_emotion("你好") is None


class TestDashboardEmotionLabels:
    """仪表盘情绪分布必须全部以中文展示。"""

    def test_covers_all_detected_emotions(self):
        from app.admin.stats import _EMOTION_LABELS
        from app.conversation.emotion_detector import (
            _EMOTION_PATTERNS,
            EMOTION_LABELS,
        )

        assert set(_EMOTION_LABELS) == set(_EMOTION_PATTERNS)
        assert set(_EMOTION_LABELS) == set(EMOTION_LABELS)

    def test_labels_are_chinese(self):
        from app.admin.stats import _EMOTION_LABELS

        for label in _EMOTION_LABELS.values():
            assert label and all("\u4e00" <= ch <= "\u9fff" for ch in label)

    def test_unknown_emotion_falls_back_to_other(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        from app.admin.stats import get_dashboard_charts

        fake_repo = AsyncMock()
        fake_repo.count_daily.return_value = []
        fake_repo.avg_latency_daily.return_value = []
        fake_repo.emotion_distribution.return_value = [
            {"emotion": "unknown", "count": 2},
        ]
        with patch("app.admin.stats.MessageRepository", return_value=fake_repo):
            charts = asyncio.run(get_dashboard_charts())
        assert charts.emotion_labels == ["其他"]
        assert charts.emotion_values == [2]
