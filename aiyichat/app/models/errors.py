"""结构化错误码：统一错误表示，便于监控与告警。

定义标准错误码枚举和 AIYIchatException 基类，
所有业务异常应使用错误码而非字符串描述。
"""

from enum import Enum
from typing import Any, Optional


class ErrorCode(Enum):
    """标准错误码枚举。

    命名规范：{模块}_{错误类型}
    """

    # LLM 相关
    LLM_TIMEOUT = "LLM_TIMEOUT"
    LLM_RATE_LIMIT = "LLM_RATE_LIMIT"
    LLM_INVALID_RESPONSE = "LLM_INVALID_RESPONSE"
    LLM_CIRCUIT_BREAKER_OPEN = "LLM_CIRCUIT_BREAKER_OPEN"
    LLM_FALLBACK_EXHAUSTED = "LLM_FALLBACK_EXHAUSTED"

    # 数据库相关
    MONGO_CONNECTION_FAILED = "MONGO_CONNECTION_FAILED"
    MONGO_SLOW_QUERY = "MONGO_SLOW_QUERY"
    MONGO_WRITE_CONFLICT = "MONGO_WRITE_CONFLICT"

    # 配额相关
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    QUOTA_REFUND_FAILED = "QUOTA_REFUND_FAILED"

    # 平台适配器相关
    PLATFORM_SEND_FAILED = "PLATFORM_SEND_FAILED"
    PLATFORM_RATE_LIMITED = "PLATFORM_RATE_LIMITED"
    PLATFORM_AUTH_FAILED = "PLATFORM_AUTH_FAILED"

    # 业务逻辑相关
    PROFILE_NOT_FOUND = "PROFILE_NOT_FOUND"
    SERVICE_NOT_FOUND = "SERVICE_NOT_FOUND"
    USER_BLOCKED = "USER_BLOCKED"
    CONVERSATION_NOT_FOUND = "CONVERSATION_NOT_FOUND"

    # 系统相关
    INTERNAL_ERROR = "INTERNAL_ERROR"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"


class AIYIchatException(Exception):
    """AIYIchat 业务异常基类。

    所有业务异常应继承此类并指定错误码，
    便于统一处理和监控。
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: Optional[dict[str, Any]] = None,
        cause: Optional[Exception] = None,
    ):
        self.code = code
        self.message = message
        self.details = details or {}
        self.cause = cause
        super().__init__(f"[{code.value}] {message}")

    def to_dict(self) -> dict[str, Any]:
        """转换为字典表示（用于 API 响应）。"""
        return {
            "error": {
                "code": self.code.value,
                "message": self.message,
                "details": self.details,
            }
        }


class LLMException(AIYIchatException):
    """LLM 调用异常。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        model: Optional[str] = None,
        latency_ms: Optional[int] = None,
        **kwargs: Any,
    ):
        details = kwargs.pop("details", {})
        if model:
            details["model"] = model
        if latency_ms:
            details["latency_ms"] = latency_ms
        super().__init__(code, message, details=details, **kwargs)


class QuotaException(AIYIchatException):
    """配额异常。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        account_id: Optional[str] = None,
        quota_type: Optional[str] = None,
        **kwargs: Any,
    ):
        details = kwargs.pop("details", {})
        if account_id:
            details["account_id"] = account_id
        if quota_type:
            details["quota_type"] = quota_type
        super().__init__(code, message, details=details, **kwargs)


class PlatformException(AIYIchatException):
    """平台适配器异常。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        platform: Optional[str] = None,
        user_id: Optional[str] = None,
        **kwargs: Any,
    ):
        details = kwargs.pop("details", {})
        if platform:
            details["platform"] = platform
        if user_id:
            details["user_id"] = user_id
        super().__init__(code, message, details=details, **kwargs)


def error_response(
    code: ErrorCode,
    message: str,
    status_code: int = 500,
    details: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """构造标准错误响应（用于 HTTP API）。

    Args:
        code: 错误码
        message: 错误描述
        status_code: HTTP 状态码
        details: 额外详情

    Returns:
        标准错误响应字典
    """
    return {
        "error": {
            "code": code.value,
            "message": message,
            "details": details or {},
        },
        "status_code": status_code,
    }
