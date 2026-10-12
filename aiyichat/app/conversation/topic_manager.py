"""话题连贯性管理模块。

追踪对话中的话题切换，为 Prompt 提供话题脉络上下文。
"""

import difflib


class TopicManager:
    """话题连贯性管理器。"""

    # 话题切换信号词
    _SHIFT_MARKERS: list[str] = [
        "对了", "顺便说", "话说", "不过", "另一件事",
        "还有", "补充一下", "突然想到", "说到这", "岔个题",
    ]

    def detect_topic_shift(self, current_msg: str, current_topic: str) -> bool:
        """检测当前消息是否包含话题切换信号。

        Args:
            current_msg: 当前用户消息
            current_topic: 当前记录的话题

        Returns:
            是否检测到话题切换
        """
        return any(marker in current_msg for marker in self._SHIFT_MARKERS)

    def detect_topic_stall(
        self,
        recent_user_msgs: list[str],
        threshold: int = 4,
        similarity: float = 0.6,
    ) -> bool:
        """检测话题是否停滞（连续多轮用户输入围绕同一话题，未引入新方向）。

        原理：计算相邻用户消息的文本相似度，若连续 N 轮相似度均高于阈值
        （即用户一直在围绕同一话题反复讨论，没有引入新话题方向），判定为话题停滞。
        此时 AI 应主动引导换话题，避免对话陷入循环。

        Args:
            recent_user_msgs: 最近的用户消息列表（按时间升序）
            threshold: 连续 N 轮同话题判定为停滞
            similarity: 用户输入相似度阈值（高于此值视为同话题）

        Returns:
            True 表示检测到话题停滞，建议主动引导换话题
        """
        if len(recent_user_msgs) < threshold:
            return False

        # 取最近 threshold 条用户消息
        window = recent_user_msgs[-threshold:]

        # 计算相邻消息的相似度，若所有相邻对都高度相似，则话题停滞
        for i in range(len(window) - 1):
            ratio = difflib.SequenceMatcher(
                None, window[i], window[i + 1]
            ).ratio()
            if ratio < similarity:
                # 存在低相似度对，说明用户引入了新话题方向，未停滞
                return False

        # 连续 N 轮相似度都高于阈值，说明话题停滞
        return True

    def generate_topic_context(
        self, topic_history: list[str], current_topic: str
    ) -> str:
        """生成话题脉络上下文提示（注入到 Prompt）。

        Args:
            topic_history: 话题历史列表
            current_topic: 当前话题

        Returns:
            话题脉络描述文本，无内容时返回空字符串
        """
        if not topic_history and not current_topic:
            return ""

        # 只取最近3个历史话题
        recent_topics = topic_history[-3:] if topic_history else []

        if not recent_topics:
            return ""

        # 生成脉络文本
        if current_topic:
            return f"【对话话题脉络】之前聊过：{'→'.join(recent_topics)}→当前：{current_topic}"
        else:
            return f"【对话话题脉络】之前聊过：{'→'.join(recent_topics)}"
