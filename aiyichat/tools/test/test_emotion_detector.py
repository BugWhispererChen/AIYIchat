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
