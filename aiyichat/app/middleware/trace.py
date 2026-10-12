"""请求级追踪中间件：为每个请求生成唯一 Trace ID。

功能：
- 为每个 HTTP 请求生成 UUID 作为 Trace ID
- 注入到日志上下文，便于关联同一请求的所有日志
- 注入到响应头 X-Trace-ID，供客户端/网关追踪
- 可选注入到 MongoDB 文档和 LLM 请求头

使用方式：
    from app.middleware.trace import TraceMiddleware, get_trace_id

    # 在 FastAPI 应用中注册
    app.add_middleware(TraceMiddleware)

    # 在代码中获取当前 Trace ID
    trace_id = get_trace_id()
"""

import logging
import uuid
from collections.abc import MutableMapping
from contextvars import ContextVar
from typing import Any, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

# ContextVar 存储当前请求的 Trace ID（协程安全）
_trace_id_ctx: ContextVar[Optional[str]] = ContextVar("trace_id", default=None)

# 响应头名称
TRACE_ID_HEADER = "X-Trace-ID"


def get_trace_id() -> Optional[str]:
    """获取当前请求的 Trace ID。

    Returns:
        当前请求的 Trace ID，如果不在请求上下文中则返回 None
    """
    return _trace_id_ctx.get()


def set_trace_id(trace_id: str) -> None:
    """设置当前请求的 Trace ID（主要用于测试）。"""
    _trace_id_ctx.set(trace_id)


def generate_trace_id() -> str:
    """生成新的 Trace ID。"""
    return str(uuid.uuid4())


class TraceIdAdapter(logging.LoggerAdapter):
    """日志适配器：自动在日志记录中注入 Trace ID。

    使用方式：
        logger = TraceIdAdapter(logging.getLogger(__name__), {})
        logger.info("message")  # 输出包含 [trace_id=xxx]
    """

    def process(self, msg: str, kwargs: MutableMapping[str, Any]) -> tuple[str, MutableMapping[str, Any]]:
        trace_id = get_trace_id()
        if trace_id:
            # 在消息前添加 trace_id 前缀
            msg = f"[trace_id={trace_id[:8]}] {msg}"
        return msg, kwargs


class TraceMiddleware(BaseHTTPMiddleware):
    """Trace ID 中间件：为每个请求生成并注入 Trace ID。"""

    async def dispatch(self, request: Request, call_next) -> Response:
        # 生成或获取 Trace ID
        trace_id = request.headers.get(TRACE_ID_HEADER) or generate_trace_id()

        # 设置到上下文
        _trace_id_ctx.set(trace_id)

        # 注入到请求状态（供路由处理函数使用）
        request.state.trace_id = trace_id

        # 处理请求
        response = await call_next(request)

        # 注入到响应头
        response.headers[TRACE_ID_HEADER] = trace_id

        return response


def get_trace_logger(name: str) -> TraceIdAdapter:
    """获取带 Trace ID 注入功能的日志记录器。

    Args:
        name: 日志记录器名称（通常使用 __name__）

    Returns:
        TraceIdAdapter 实例
    """
    return TraceIdAdapter(logging.getLogger(name), {})


def inject_trace_to_mongo_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """将当前 Trace ID 注入到 MongoDB 文档（可选）。

    Args:
        doc: 要插入/更新的 MongoDB 文档

    Returns:
        注入 trace_id 后的文档
    """
    trace_id = get_trace_id()
    if trace_id:
        doc["trace_id"] = trace_id
    return doc


def get_llm_request_headers() -> dict[str, str]:
    """获取 LLM 请求头（包含 Trace ID，供 DashScope 侧排查）。

    Returns:
        包含 X-Request-ID 的请求头字典
    """
    trace_id = get_trace_id()
    if trace_id:
        return {"X-Request-ID": trace_id}
    return {}
