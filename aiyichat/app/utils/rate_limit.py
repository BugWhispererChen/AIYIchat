"""IP 维度滑动窗口限流（单实例内存实现）。

零依赖、低延迟，适用于单容器部署：
- Docker 容器重启即清空计数；
- **多实例部署时各实例独立计数，限流会失效**，此时必须改为 Redis 实现
  （`INCR` + `EXPIRE`，key 格式 `rate_limit:register:{ip}` / `rate_limit:login:{ip}`），
  详见 README「部署与运维」章节。
"""

import threading
import time
from collections import deque
from typing import Optional

# 窗口内请求记录的最大 key 数：超出按最早写入淘汰，防止被伪造 IP 撑爆内存
_MAX_KEYS = 20000


class SlidingWindowLimiter:
    """按 key（一般是 IP）统计滑动窗口内的请求次数。

    用单调时钟（`time.monotonic`）记录时间戳，避免系统时钟回拨导致窗口计算错乱。
    FastAPI 的 `async def` 可能在线程池中执行同步依赖，故写路径加锁。
    """

    def __init__(self, max_keys: int = _MAX_KEYS) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._max_keys = max_keys

    def allow(self, key: str, limit: int, window_seconds: float) -> bool:
        """本次请求是否放行。

        Args:
            key: 限流维度（通常为客户端 IP）
            limit: 窗口内允许的最大请求数，<=0 表示不限流
            window_seconds: 滑动窗口长度（秒）
        """
        if limit <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                if len(self._hits) >= self._max_keys:
                    # 淘汰最早写入的 key（dict 保持插入顺序）
                    self._hits.pop(next(iter(self._hits)), None)
                hits = deque()
                self._hits[key] = hits
            self._drop_old(hits, now, window_seconds)
            if len(hits) >= limit:
                return False
            hits.append(now)
            return True

    def retry_after(self, key: str, window_seconds: float) -> int:
        """距最早记录滑出窗口的剩余秒数（供 429 的 Retry-After 头使用）。"""
        now = time.monotonic()
        with self._lock:
            hits = self._hits.get(key)
            if not hits:
                return 0
            remain = window_seconds - (now - hits[0])
            return max(1, int(remain + 0.999))

    def reset(self, key: Optional[str] = None) -> None:
        """清空计数：指定 key 则只清该 key，否则整体清空（测试与运维手动解封）。"""
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)

    @staticmethod
    def _drop_old(hits: deque[float], now: float, window_seconds: float) -> None:
        """弹出已滑出窗口的时间戳（队列头部即最早记录）。"""
        cutoff = now - window_seconds
        while hits and hits[0] <= cutoff:
            hits.popleft()


# 窗口长度：10 分钟。注册与登录分别计数，避免登录爆破把注册通道一起打死
WINDOW_SECONDS = 600.0

# 模块级单例：注册/登录共用进程内计数
register_limiter = SlidingWindowLimiter()
login_limiter = SlidingWindowLimiter()
# 后台管理员登录单独计数：不能与 C 端登录共用，否则前台爆破会误伤后台
admin_login_limiter = SlidingWindowLimiter()
# 账号中心写操作（改密 / 资料 / 清理 / 注销）共用一个计数器：
# key 已含 scope 与凭据 ID，不同动作与不同账号互不影响，只需注意别把同 IP 的其他账号打死
account_limiter = SlidingWindowLimiter()

# 账号中心的限流作用域：统一走 account_limiter，避免每新增一个端点就新增一个单例
_ACCOUNT_SCOPES = frozenset(
    {
        "account_password",
        "account_write",
        "account_data",
        "account_delete",
        "web_bot_write",
        "web_bot_qr",
        # 扫码轮询与取码分开计数：轮询频率天然远高于取码，共用阈值会让绑定流程
        # 在二维码有效期内必然撞上 429
        "web_bot_poll",
        # 图形验证码：每次调用都要生成图片并在内存挂一份答案，不限流就是免费刷内存的入口
        "captcha",
    }
)


def check_rate_limit(scope: str, ip: str, limit: int) -> None:
    """限流检查，超限抛出 `RateLimited`。"""
    limiter = _limiter_for(scope)
    if not limiter.allow(f"{ip}|{scope}", limit, WINDOW_SECONDS):
        raise RateLimited(
            retry_after=limiter.retry_after(f"{ip}|{scope}", WINDOW_SECONDS),
            scope=scope,
        )


def _limiter_for(scope: str) -> SlidingWindowLimiter:
    """按业务动作取对应限流器。"""
    if scope == "register":
        return register_limiter
    if scope == "login":
        return login_limiter
    if scope == "admin_login":
        return admin_login_limiter
    if scope in _ACCOUNT_SCOPES:
        return account_limiter
    raise ValueError(f"未注册的限流作用域: {scope}")


class RateLimited(Exception):
    """触发限流：由 API 层转成 429 响应。"""

    def __init__(self, retry_after: int, scope: str) -> None:
        super().__init__(f"请求过于频繁，请 {retry_after} 秒后重试")
        self.retry_after = retry_after
        self.scope = scope
        self.message = f"请求过于频繁，请稍后重试（{scope}）"
