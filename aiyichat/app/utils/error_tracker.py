"""错误分类与聚合：避免日志洪水，识别系统性故障。

核心机制：
- 错误指纹：基于异常类型 + 关键堆栈帧生成唯一标识
- 相同指纹的错误在聚合窗口内仅记录 1 次详细日志，后续计数
- 计数超过阈值时触发告警（可扩展到邮件/Webhook）

使用方式：
    from app.utils.error_tracker import track_error

    try:
        ...
    except Exception as e:
        track_error(e, context={"identity_id": "123"})
"""

import hashlib
import logging
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 聚合窗口：相同指纹错误在此时间内仅记录一次详细日志
_AGGREGATION_WINDOW_SECONDS = 300  # 5 分钟

# 告警阈值：窗口内错误次数超过此值触发告警
_ALERT_THRESHOLD = 10


@dataclass
class ErrorRecord:
    """错误记录。"""

    fingerprint: str
    exception_type: str
    message: str
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    count: int = 1
    context: dict[str, Any] = field(default_factory=dict)
    stack_trace: str = ""
    alerted: bool = False


class ErrorTracker:
    """错误追踪器：分类、聚合、告警。"""

    def __init__(
        self,
        aggregation_window: int = _AGGREGATION_WINDOW_SECONDS,
        alert_threshold: int = _ALERT_THRESHOLD,
    ):
        self.aggregation_window = aggregation_window
        self.alert_threshold = alert_threshold
        self._errors: dict[str, ErrorRecord] = {}
        self._lock = asyncio.Lock()

    def _generate_fingerprint(
        self,
        exc: Exception,
        context: Optional[dict[str, Any]] = None,
    ) -> str:
        """生成错误指纹。

        基于异常类型 + 关键堆栈帧 + 可选上下文生成唯一标识。
        相同指纹的错误被视为同一类问题。
        """
        # 异常类型
        exc_type = type(exc).__name__

        # 提取关键堆栈帧（最底层的 3 个业务代码帧）
        tb = traceback.extract_tb(exc.__traceback__)
        key_frames = []
        for frame in tb[-5:]:  # 取最后 5 帧
            # 过滤掉标准库/第三方库帧，保留业务代码帧
            if "app/" in frame.filename or "app\\" in frame.filename:
                key_frames.append(f"{frame.filename}:{frame.lineno}")

        # 上下文关键信息（如 identity_id、service_id）
        context_key = ""
        if context:
            # 只取关键字段，避免上下文变化导致指纹不同
            key_fields = ["identity_id", "service_id", "bot_id", "platform"]
            context_parts = [
                f"{k}={v}" for k, v in context.items() if k in key_fields
            ]
            context_key = "|".join(context_parts)

        # 组合并哈希
        raw = f"{exc_type}|{'|'.join(key_frames)}|{context_key}"
        return hashlib.md5(raw.encode()).hexdigest()[:16]

    async def track(
        self,
        exc: Exception,
        context: Optional[dict[str, Any]] = None,
        *,
        force_log: bool = False,
    ) -> ErrorRecord:
        """追踪错误。

        Args:
            exc: 异常对象
            context: 上下文信息（如 user_id、service_id）
            force_log: 是否强制记录详细日志（忽略聚合窗口）

        Returns:
            ErrorRecord 错误记录
        """
        fingerprint = self._generate_fingerprint(exc, context)
        now = time.time()

        async with self._lock:
            if fingerprint in self._errors:
                record = self._errors[fingerprint]
                record.last_seen = now
                record.count += 1

                # 检查是否需要告警
                if (
                    not record.alerted
                    and record.count >= self.alert_threshold
                ):
                    record.alerted = True
                    await self._send_alert(record)

                # 聚合窗口内不重复记录详细日志
                if not force_log and (now - record.first_seen) < self.aggregation_window:
                    logger.debug(
                        "Error aggregated (fingerprint=%s, count=%d): %s",
                        fingerprint,
                        record.count,
                        str(exc),
                    )
                    return record

                # 超过聚合窗口，重置并记录新详细日志
                record.first_seen = now
                record.count = 1
                record.alerted = False
            else:
                # 新错误
                record = ErrorRecord(
                    fingerprint=fingerprint,
                    exception_type=type(exc).__name__,
                    message=str(exc),
                    context=context or {},
                    stack_trace=traceback.format_exc(),
                )
                self._errors[fingerprint] = record

            # 记录详细日志
            logger.error(
                "Error tracked (fingerprint=%s, type=%s, count=%d): %s\n%s",
                fingerprint,
                record.exception_type,
                record.count,
                record.message,
                record.stack_trace,
                exc_info=True,
            )

            return record

    async def _send_alert(self, record: ErrorRecord) -> None:
        """发送告警（可扩展到邮件/Webhook）。

        当前实现：记录告警日志，可扩展为发送到管理后台/邮件。
        """
        logger.critical(
            "ERROR ALERT: fingerprint=%s, type=%s, count=%d in %ds, message=%s",
            record.fingerprint,
            record.exception_type,
            record.count,
            int(record.last_seen - record.first_seen),
            record.message,
        )

    async def get_stats(self) -> dict[str, Any]:
        """获取错误统计信息。"""
        async with self._lock:
            now = time.time()
            active_errors = [
                {
                    "fingerprint": r.fingerprint,
                    "type": r.exception_type,
                    "message": r.message[:100],
                    "count": r.count,
                    "first_seen": r.first_seen,
                    "last_seen": r.last_seen,
                    "age_seconds": int(now - r.first_seen),
                }
                for r in self._errors.values()
                if now - r.last_seen < self.aggregation_window * 2  # 最近 2 个窗口内活跃
            ]
            return {
                "total_tracked": len(self._errors),
                "active_errors": active_errors,
                "aggregation_window": self.aggregation_window,
                "alert_threshold": self.alert_threshold,
            }


# 全局错误追踪器实例
_error_tracker: Optional[ErrorTracker] = None


def get_error_tracker() -> ErrorTracker:
    """获取全局错误追踪器实例。"""
    global _error_tracker
    if _error_tracker is None:
        _error_tracker = ErrorTracker()
    return _error_tracker


async def track_error(
    exc: Exception,
    context: Optional[dict[str, Any]] = None,
    *,
    force_log: bool = False,
) -> ErrorRecord:
    """追踪错误的便捷函数。

    Args:
        exc: 异常对象
        context: 上下文信息
        force_log: 是否强制记录详细日志

    Returns:
        ErrorRecord 错误记录
    """
    return await get_error_tracker().track(exc, context, force_log=force_log)



# 导入 asyncio 用于类型注解
import asyncio  # noqa: E402
