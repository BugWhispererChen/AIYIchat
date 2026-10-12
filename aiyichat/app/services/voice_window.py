"""语音回复动态触发窗口管理模块。

在"用户发语音才回语音"的单一触发之上，提供按会话独立的动态窗口：
用户发送语音触发语音回复后，该会话在随机时长（默认 5-30 分钟）的
固定窗口内，文字消息也以语音回复；窗口到期自动回落为仅语音触发。

设计要点：
- 固定窗口：窗口由"用户发语音"开启，窗口内的语音回复不续期，
  到期需用户再发语音重新激活
- 按会话独立：以 conversation_id 为键，群聊/单聊天然隔离
- 进程内内存态：窗口短且非关键状态，不落库；服务重启后清零，
  符合"重新激活"语义
"""

import logging
import random
import time

logger = logging.getLogger(__name__)


class VoiceWindowManager:
    """按会话维护语音回复窗口的内存状态。"""

    def __init__(self) -> None:
        # conversation_id -> 窗口到期时间戳（time.time()）
        self._windows: dict[str, float] = {}

    def is_active(self, conv_id: str) -> bool:
        """判断会话当前是否处于语音窗口内（惰性清理过期键）。"""
        if not conv_id:
            return False
        expire_at = self._windows.get(conv_id)
        if expire_at is None:
            return False
        if time.time() >= expire_at:
            # 已过期，惰性删除避免内存泄漏
            del self._windows[conv_id]
            return False
        return True

    def touch(self, conv_id: str, min_minutes: int, max_minutes: int) -> float:
        """为会话开启语音窗口（固定窗口：已在窗口内则不续期）。

        Args:
            conv_id: 会话 ID，为空时直接跳过
            min_minutes: 窗口下限（分钟）
            max_minutes: 窗口上限（分钟）

        Returns:
            窗口到期时间戳；未开启（空 conv_id 或已在窗口内）时返回 0.0
        """
        if not conv_id:
            return 0.0
        if self.is_active(conv_id):
            # 固定窗口语义：窗口内不续期
            return 0.0
        # 防御：下限不大于上限，且不为负
        lo = max(0, min_minutes)
        hi = max(lo, max_minutes)
        duration = random.uniform(lo, hi) * 60
        expire_at = time.time() + duration
        self._windows[conv_id] = expire_at
        logger.info(
            "会话 %s 开启语音窗口，时长 %.1f 分钟", conv_id, duration / 60
        )
        return expire_at


# 模块级单例，供各平台适配器共享
voice_window = VoiceWindowManager()
