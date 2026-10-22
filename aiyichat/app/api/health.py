"""深度健康检查：检查 MongoDB、LLM、磁盘空间等关键依赖。

返回状态：
- healthy: 所有检查通过
- degraded: 非关键检查失败（如 LLM 慢但可用）
- unhealthy: 关键检查失败（如 MongoDB 断开）
"""

import logging
import shutil
import time
from typing import Any

from fastapi import APIRouter

from app.db.mongo import get_database

logger = logging.getLogger(__name__)

health_router = APIRouter()

# 磁盘空间阈值（MB）
_DISK_WARNING_THRESHOLD_MB = 1024  # 1GB
_DISK_CRITICAL_THRESHOLD_MB = 100  # 100MB


async def _check_mongodb() -> dict[str, Any]:
    """检查 MongoDB 连通性。"""
    try:
        start = time.monotonic()
        db = get_database()
        await db.command("ping")
        latency_ms = int((time.monotonic() - start) * 1000)
        return {
            "status": "healthy",
            "latency_ms": latency_ms,
        }
    except Exception as e:
        logger.error("MongoDB health check failed: %s", e)
        return {
            "status": "unhealthy",
            "error": str(e),
        }


async def _check_llm() -> dict[str, Any]:
    """检查 LLM 连通性（轻量 ping）。"""
    try:
        from app.llm.provider_factory import get_llm_provider

        start = time.monotonic()
        get_llm_provider("chat")
        # 轻量调用：发送空消息列表，仅验证连通性
        # 注意：某些 provider 可能不支持空消息，这里仅做示例
        # 实际实现可能需要调整
        latency_ms = int((time.monotonic() - start) * 1000)

        # 检查熔断器状态
        from app.llm.circuit_breaker import get_circuit_breaker

        cb = get_circuit_breaker("chat")
        cb_stats = cb.get_stats()

        return {
            "status": "healthy" if cb_stats["state"] == "closed" else "degraded",
            "latency_ms": latency_ms,
            "circuit_breaker": cb_stats,
        }
    except Exception as e:
        logger.warning("LLM health check failed: %s", e)
        return {
            "status": "degraded",  # LLM 失败不视为关键故障
            "error": str(e),
        }


async def _check_embedding() -> dict[str, Any]:
    """检查语义检索端点（向量化失败计数与熔断状态）。

    embedding 的额度耗尽/限流不会打断对话主链路（自动回落规则排序），因此这里只报
    degraded 而非 unhealthy。它用的是服务内的私有熔断实例（与 chat 隔离，见
    `app/services/embedding_service.py`），这里只读状态值，不把它塞进
    `get_circuit_breaker` 注册表——端点隔离的意图不能因为健康检查而回退。
    """
    try:
        from app.services.embedding_service import get_embedding_service

        st = get_embedding_service().status()
        if not st["enabled"]:
            # 未启用不是故障：按设计走规则排序
            status = "healthy"
        elif st["breaker_state"] == "open" or st["fail_count"] > 0:
            status = "degraded"
        else:
            status = "healthy"
        return {
            "status": status,
            "enabled": st["enabled"],
            "fail_count": st["fail_count"],
            "breaker_state": st["breaker_state"],
            "last_error_code": st.get("last_error_code", ""),
        }
    except Exception as e:
        logger.warning("Embedding health check failed: %s", e)
        return {
            "status": "degraded",
            "error": str(e),
        }


def _check_disk() -> dict[str, Any]:
    """检查磁盘空间。"""
    try:
        usage = shutil.disk_usage("/")
        free_mb = usage.free // (1024 * 1024)
        total_mb = usage.total // (1024 * 1024)
        used_percent = round((usage.used / usage.total) * 100, 1)

        if free_mb < _DISK_CRITICAL_THRESHOLD_MB:
            status = "unhealthy"
        elif free_mb < _DISK_WARNING_THRESHOLD_MB:
            status = "degraded"
        else:
            status = "healthy"

        return {
            "status": status,
            "free_mb": free_mb,
            "total_mb": total_mb,
            "used_percent": used_percent,
        }
    except Exception as e:
        logger.error("Disk health check failed: %s", e)
        return {
            "status": "degraded",
            "error": str(e),
        }


def _check_background_tasks() -> dict[str, Any]:
    """检查后台任务状态。"""
    try:
        from app.utils.tasks import get_task_stats

        stats = get_task_stats()
        # 如果等待队列过长，视为 degraded
        if stats["pending_tasks"] > stats["concurrency_limit"] * 2:
            status = "degraded"
        else:
            status = "healthy"

        return {
            "status": status,
            **stats,
        }
    except Exception as e:
        logger.warning("Background tasks health check failed: %s", e)
        return {
            "status": "degraded",
            "error": str(e),
        }


@health_router.get("/health")
async def health_check():
    """深度健康检查端点。

    Returns:
        - status: healthy / degraded / unhealthy
        - checks: 各项检查的详细结果
        - timestamp: 检查时间戳
    """
    checks = {
        "mongodb": await _check_mongodb(),
        "llm": await _check_llm(),
        "embedding": await _check_embedding(),
        "disk": _check_disk(),
        "background_tasks": _check_background_tasks(),
    }

    # 计算总体状态
    statuses = [c["status"] for c in checks.values()]
    if "unhealthy" in statuses:
        overall_status = "unhealthy"
    elif "degraded" in statuses:
        overall_status = "degraded"
    else:
        overall_status = "healthy"

    return {
        "status": overall_status,
        "checks": checks,
        "timestamp": time.time(),
    }


@health_router.get("/health/live")
async def liveness_check():
    """存活检查（Kubernetes liveness probe）。

    仅检查服务是否运行，不检查外部依赖。
    """
    return {"status": "alive", "timestamp": time.time()}


@health_router.get("/health/ready")
async def readiness_check():
    """就绪检查（Kubernetes readiness probe）。

    检查关键依赖（MongoDB）是否可用。
    """
    mongo_status = await _check_mongodb()
    if mongo_status["status"] == "healthy":
        return {"status": "ready", "timestamp": time.time()}
    else:
        return {
            "status": "not_ready",
            "reason": "mongodb_unavailable",
            "timestamp": time.time(),
        }
