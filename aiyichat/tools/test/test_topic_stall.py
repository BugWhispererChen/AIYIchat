"""话题停滞检测模块测试。

覆盖：
- detect_topic_stall：连续多轮同话题判定
- detect_topic_stall：新话题引入时未停滞
- detect_topic_stall：消息数不足阈值时未停滞
- detect_topic_stall：边界条件（空列表、单条消息）
"""

from app.conversation.topic_manager import TopicManager


class TestTopicStallDetection:
    """话题停滞检测。"""

    def test_stall_detected(self) -> None:
        """连续多轮高度相似的身份输入判定为停滞。"""
        tm = TopicManager()
        # 4 轮高度相似的输入（围绕同一话题，仅少量字差异）
        msgs = [
            "今天工作好累啊",
            "今天工作好累呀",
            "今天工作好累呢",
            "今天工作好累哦",
        ]
        assert tm.detect_topic_stall(msgs, threshold=4, similarity=0.6) is True

    def test_no_stall_with_new_topic(self) -> None:
        """引入新话题时未停滞。"""
        tm = TopicManager()
        msgs = [
            "今天工作好累啊",
            "是啊今天工作真的好累",
            "对了，周末去看电影吧",  # 新话题
            "最近有什么好看的电影",
        ]
        assert tm.detect_topic_stall(msgs, threshold=4, similarity=0.6) is False

    def test_no_stall_insufficient_messages(self) -> None:
        """消息数不足阈值时未停滞。"""
        tm = TopicManager()
        msgs = ["今天工作好累啊", "是啊今天工作真的好累"]
        assert tm.detect_topic_stall(msgs, threshold=4, similarity=0.6) is False

    def test_no_stall_empty(self) -> None:
        """空列表未停滞。"""
        tm = TopicManager()
        assert tm.detect_topic_stall([], threshold=4, similarity=0.6) is False

    def test_no_stall_single_message(self) -> None:
        """单条消息未停滞。"""
        tm = TopicManager()
        assert tm.detect_topic_stall(["你好"], threshold=4, similarity=0.6) is False

    def test_stall_with_exact_repeats(self) -> None:
        """完全重复的消息判定为停滞。"""
        tm = TopicManager()
        msgs = ["你好"] * 4
        assert tm.detect_topic_stall(msgs, threshold=4, similarity=0.6) is True

    def test_stall_threshold_boundary(self) -> None:
        """阈值边界：恰好达到阈值时判定为停滞。"""
        tm = TopicManager()
        # 3 条消息，threshold=3
        msgs = ["今天天气不错", "今天天气真好", "今天天气很好"]
        assert tm.detect_topic_stall(msgs, threshold=3, similarity=0.6) is True
        # 3 条消息，threshold=4 → 不足
        assert tm.detect_topic_stall(msgs, threshold=4, similarity=0.6) is False

    def test_stall_similarity_boundary(self) -> None:
        """相似度边界：低于阈值时未停滞。"""
        tm = TopicManager()
        # 相似度较低的消息序列
        msgs = [
            "今天工作好累",
            "明天想去爬山",
            "最近看了部电影",
            "晚上吃什么好呢",
        ]
        assert tm.detect_topic_stall(msgs, threshold=4, similarity=0.6) is False
