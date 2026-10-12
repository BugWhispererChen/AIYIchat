import logging
import time
from collections import defaultdict

from app.config import settings

logger = logging.getLogger(__name__)


class RateLimiter:
    """简单的内存级用户限频器（按分钟滑动窗口）"""

    def __init__(self):
        self._window = 60.0  # 1分钟窗口
        self._requests: dict[str, list[float]] = defaultdict(list)

    def is_allowed(self, user_id: str) -> bool:
        now = time.monotonic()
        window_start = now - self._window

        # 清理过期记录
        self._requests[user_id] = [
            t for t in self._requests[user_id] if t > window_start
        ]

        if len(self._requests[user_id]) >= settings.rate_limit_per_user:
            logger.warning("Rate limit exceeded for user %s", user_id)
            return False

        self._requests[user_id].append(now)
        return True

    def get_remaining(self, user_id: str) -> int:
        now = time.monotonic()
        window_start = now - self._window
        recent = [t for t in self._requests.get(user_id, []) if t > window_start]
        return max(0, settings.rate_limit_per_user - len(recent))


# 全局限频器实例
rate_limiter = RateLimiter()
