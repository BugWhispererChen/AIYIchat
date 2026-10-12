"""LLM 调用熔断器：防止故障扩散，实现快速失败与自动恢复。

核心机制：
- 滑动窗口统计最近 N 次调用的失败率
- 失败率超过阈值时触发熔断，直接抛出 CircuitBreakerOpen 异常
- 熔断后间隔 recovery_timeout 秒进入半开状态，允许少量试探请求
- 半开成功则关闭熔断，失败则延长恢复期
"""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """熔断器状态。"""

    CLOSED = "closed"  # 正常闭合，请求通过
    OPEN = "open"  # 熔断开启，请求被拒绝
    HALF_OPEN = "half_open"  # 半开试探，允许少量请求验证恢复


class CircuitBreakerOpen(Exception):
    """熔断器开启时抛出的异常。"""

    def __init__(self, message: str = "LLM service circuit breaker is open"):
        self.message = message
        super().__init__(self.message)


@dataclass
class CircuitBreakerConfig:
    """熔断器配置。"""

    failure_threshold: float = 0.5  # 失败率阈值（0.0~1.0）
    window_size: int = 100  # 滑动窗口大小
    recovery_timeout: float = 30.0  # 熔断后恢复试探间隔（秒）
    half_open_max_calls: int = 3  # 半开状态最大试探请求数
    success_threshold: int = 2  # 半开状态关闭熔断所需连续成功次数


@dataclass
class CircuitBreakerStats:
    """熔断器统计信息。"""

    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: float = 0.0
    last_state_change: float = field(default_factory=time.monotonic)
    half_open_calls: int = 0
    half_open_successes: int = 0


class CircuitBreaker:
    """LLM 调用熔断器。

    使用滑动窗口统计失败率，超过阈值时自动熔断，
    防止故障扩散到整个系统。
    """

    def __init__(self, config: CircuitBreakerConfig | None = None):
        self.config = config or CircuitBreakerConfig()
        self.stats = CircuitBreakerStats()
        self._window: deque[bool] = deque(maxlen=self.config.window_size)
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        """当前熔断器状态。"""
        return self.stats.state

    @property
    def failure_rate(self) -> float:
        """当前滑动窗口内的失败率。"""
        if not self._window:
            return 0.0
        failures = sum(1 for success in self._window if not success)
        return failures / len(self._window)

    async def call(
        self,
        func: Callable[..., Coroutine[Any, Any, Any]],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """通过熔断器调用异步函数。

        Args:
            func: 要调用的异步函数
            *args: 位置参数
            **kwargs: 关键字参数

        Returns:
            函数返回值

        Raises:
            CircuitBreakerOpen: 熔断器开启时抛出
            Exception: 函数本身抛出的异常
        """
        async with self._lock:
            self._check_state_transition()

            if self.stats.state == CircuitState.OPEN:
                raise CircuitBreakerOpen(
                    f"Circuit breaker is OPEN, "
                    f"recovery in {self._time_until_half_open():.1f}s"
                )

            if self.stats.state == CircuitState.HALF_OPEN:
                if self.stats.half_open_calls >= self.config.half_open_max_calls:
                    raise CircuitBreakerOpen(
                        "Circuit breaker is HALF_OPEN, max probe calls reached"
                    )
                self.stats.half_open_calls += 1

        # 执行实际调用
        try:
            result = await func(*args, **kwargs)
            await self._on_success()
            return result
        except Exception as e:
            await self._on_failure(e)
            raise

    def _check_state_transition(self) -> None:
        """检查并执行状态转换。"""
        now = time.monotonic()

        if self.stats.state == CircuitState.OPEN:
            # 检查是否可以进入半开状态
            if now - self.stats.last_failure_time >= self.config.recovery_timeout:
                self._transition_to_half_open()

        elif self.stats.state == CircuitState.HALF_OPEN:
            # 半开状态在 _on_success/_on_failure 中处理转换
            pass

    def _transition_to_half_open(self) -> None:
        """转换到半开状态。"""
        self.stats.state = CircuitState.HALF_OPEN
        self.stats.half_open_calls = 0
        self.stats.half_open_successes = 0
        self.stats.last_state_change = time.monotonic()
        logger.info("Circuit breaker transitioned to HALF_OPEN")

    def _transition_to_closed(self) -> None:
        """转换到闭合状态。"""
        self.stats.state = CircuitState.CLOSED
        self.stats.last_state_change = time.monotonic()
        self._window.clear()
        logger.info("Circuit breaker transitioned to CLOSED")

    def _transition_to_open(self) -> None:
        """转换到开启状态。"""
        self.stats.state = CircuitState.OPEN
        self.stats.last_state_change = time.monotonic()
        logger.warning(
            "Circuit breaker transitioned to OPEN, failure_rate=%.2f",
            self.failure_rate,
        )

    async def _on_success(self) -> None:
        """记录成功调用。"""
        async with self._lock:
            self._window.append(True)
            self.stats.success_count += 1

            if self.stats.state == CircuitState.HALF_OPEN:
                self.stats.half_open_successes += 1
                if (
                    self.stats.half_open_successes
                    >= self.config.success_threshold
                ):
                    self._transition_to_closed()

    async def _on_failure(self, error: Exception) -> None:
        """记录失败调用。"""
        async with self._lock:
            self._window.append(False)
            self.stats.failure_count += 1
            self.stats.last_failure_time = time.monotonic()

            if self.stats.state == CircuitState.HALF_OPEN:
                # 半开状态失败，直接重新开启
                self._transition_to_open()
            elif self.stats.state == CircuitState.CLOSED:
                # 检查失败率是否超过阈值（窗口未满一半时不触发，避免冷启动误熔断）
                min_samples = max(2, self.config.window_size // 2)
                if (
                    len(self._window) >= min_samples
                    and self.failure_rate >= self.config.failure_threshold
                ):
                    self._transition_to_open()

    def _time_until_half_open(self) -> float:
        """距离进入半开状态的剩余时间（秒）。"""
        if self.stats.state != CircuitState.OPEN:
            return 0.0
        elapsed = time.monotonic() - self.stats.last_failure_time
        return max(0.0, self.config.recovery_timeout - elapsed)

    def get_stats(self) -> dict[str, Any]:
        """获取熔断器统计信息（用于监控）。"""
        return {
            "state": self.stats.state.value,
            "failure_rate": round(self.failure_rate, 4),
            "failure_count": self.stats.failure_count,
            "success_count": self.stats.success_count,
            "window_size": len(self._window),
            "last_state_change": self.stats.last_state_change,
        }

    def reset(self) -> None:
        """重置熔断器（用于测试或手动恢复）。"""
        self.stats = CircuitBreakerStats()
        self._window.clear()
        logger.info("Circuit breaker reset")


# 全局熔断器实例（按任务类型隔离）
_circuit_breakers: dict[str, CircuitBreaker] = {}


def get_circuit_breaker(task: str = "default") -> CircuitBreaker:
    """获取指定任务的熔断器实例。

    Args:
        task: 任务类型标识（如 "chat", "summary", "focus"）

    Returns:
        该任务对应的 CircuitBreaker 实例
    """
    if task not in _circuit_breakers:
        _circuit_breakers[task] = CircuitBreaker()
    return _circuit_breakers[task]


def reset_all_circuit_breakers() -> None:
    """重置所有熔断器（用于测试）。"""
    global _circuit_breakers
    _circuit_breakers = {}
