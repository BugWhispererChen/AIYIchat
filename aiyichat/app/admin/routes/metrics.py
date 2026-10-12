"""业务级 Metrics 端点：暴露 LLM、记忆、主动消息等关键业务指标。

提供 /admin/api/business_metrics 端点，供管理后台可视化展示。
"""

import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.admin.auth import require_admin

logger = logging.getLogger(__name__)

# 内存滑动窗口存储（1 小时）
_WINDOW_SIZE = 3600  # 秒


@dataclass
class LLMCallMetric:
    """LLM 调用指标。"""

    timestamp: float
    model: str
    latency_ms: int
    success: bool
    error_type: str = ""


@dataclass
class MemoryMetric:
    """记忆系统指标。"""

    timestamp: float
    service_id: str
    injected_count: int
    hit_count: int  # 被回复引用的数量
    tier_distribution: dict[str, int] = field(default_factory=dict)


@dataclass
class ProactiveMetric:
    """主动消息指标。"""

    timestamp: float
    stage: int
    success: bool
    rate_limited: bool = False


class BusinessMetrics:
    """业务指标收集器（内存滑动窗口）。"""

    def __init__(self, window_size: int = _WINDOW_SIZE):
        self.window_size = window_size
        self._llm_calls: deque[LLMCallMetric] = deque(maxlen=10000)
        self._memory_ops: deque[MemoryMetric] = deque(maxlen=10000)
        self._proactive_msgs: deque[ProactiveMetric] = deque(maxlen=10000)
        self._start_time = time.time()

    def record_llm_call(
        self,
        model: str,
        latency_ms: int,
        success: bool,
        error_type: str = "",
    ) -> None:
        """记录 LLM 调用。"""
        self._llm_calls.append(
            LLMCallMetric(
                timestamp=time.time(),
                model=model,
                latency_ms=latency_ms,
                success=success,
                error_type=error_type,
            )
        )

    def record_memory_op(
        self,
        service_id: str,
        injected_count: int,
        hit_count: int,
        tier_distribution: dict[str, int] | None = None,
    ) -> None:
        """记录记忆操作。"""
        self._memory_ops.append(
            MemoryMetric(
                timestamp=time.time(),
                service_id=service_id,
                injected_count=injected_count,
                hit_count=hit_count,
                tier_distribution=tier_distribution or {},
            )
        )

    def record_proactive_msg(
        self,
        stage: int,
        success: bool,
        rate_limited: bool = False,
    ) -> None:
        """记录主动消息。"""
        self._proactive_msgs.append(
            ProactiveMetric(
                timestamp=time.time(),
                stage=stage,
                success=success,
                rate_limited=rate_limited,
            )
        )

    def _filter_window(self, items: deque, window_seconds: int) -> list:
        """过滤指定时间窗口内的数据。"""
        cutoff = time.time() - window_seconds
        return [item for item in items if item.timestamp >= cutoff]

    def get_llm_stats(self, window_seconds: int = 3600) -> dict[str, Any]:
        """获取 LLM 统计。"""
        calls = self._filter_window(self._llm_calls, window_seconds)
        if not calls:
            return {"total": 0, "qps": 0, "latency": {}, "error_rate": 0}

        total = len(calls)
        qps = total / window_seconds
        latencies = [c.latency_ms for c in calls]
        latencies.sort()
        errors = sum(1 for c in calls if not c.success)

        # 按模型分组
        by_model: dict[str, list[int]] = defaultdict(list)
        for c in calls:
            by_model[c.model].append(c.latency_ms)

        return {
            "total": total,
            "qps": round(qps, 2),
            "latency": {
                "p50": latencies[len(latencies) // 2] if latencies else 0,
                "p95": latencies[int(len(latencies) * 0.95)] if latencies else 0,
                "p99": latencies[int(len(latencies) * 0.99)] if latencies else 0,
                "avg": sum(latencies) // len(latencies) if latencies else 0,
            },
            "error_rate": round(errors / total, 4) if total else 0,
            "by_model": {
                m: {
                    "count": len(lats),
                    "avg_latency": sum(lats) // len(lats) if lats else 0,
                }
                for m, lats in by_model.items()
            },
        }

    def get_memory_stats(self, window_seconds: int = 3600) -> dict[str, Any]:
        """获取记忆系统统计。"""
        ops = self._filter_window(self._memory_ops, window_seconds)
        if not ops:
            return {"total_ops": 0, "hit_rate": 0, "tier_distribution": {}}

        total_injected = sum(op.injected_count for op in ops)
        total_hit = sum(op.hit_count for op in ops)

        # 聚合分层分布
        tier_dist: dict[str, int] = defaultdict(int)
        for op in ops:
            for tier, count in op.tier_distribution.items():
                tier_dist[tier] += count

        return {
            "total_ops": len(ops),
            "total_injected": total_injected,
            "total_hit": total_hit,
            "hit_rate": round(total_hit / total_injected, 4) if total_injected else 0,
            "tier_distribution": dict(tier_dist),
        }

    def get_proactive_stats(self, window_seconds: int = 3600) -> dict[str, Any]:
        """获取主动消息统计。"""
        msgs = self._filter_window(self._proactive_msgs, window_seconds)
        if not msgs:
            return {"total": 0, "success_rate": 0, "rate_limit_rate": 0}

        total = len(msgs)
        success = sum(1 for m in msgs if m.success)
        rate_limited = sum(1 for m in msgs if m.rate_limited)

        # 按阶段分布
        by_stage: dict[int, int] = defaultdict(int)
        for m in msgs:
            by_stage[m.stage] += 1

        return {
            "total": total,
            "success_rate": round(success / total, 4) if total else 0,
            "rate_limit_rate": round(rate_limited / total, 4) if total else 0,
            "by_stage": dict(by_stage),
        }

    def get_all_stats(self) -> dict[str, Any]:
        """获取所有业务指标。"""
        return {
            "uptime_seconds": int(time.time() - self._start_time),
            "llm": self.get_llm_stats(),
            "memory": self.get_memory_stats(),
            "proactive": self.get_proactive_stats(),
        }


# 全局指标收集器实例
_metrics: BusinessMetrics | None = None


def get_business_metrics() -> BusinessMetrics:
    """获取全局业务指标收集器。"""
    global _metrics
    if _metrics is None:
        _metrics = BusinessMetrics()
    return _metrics


# 路由定义
metrics_router = APIRouter(prefix="/api", tags=["metrics"])


@metrics_router.get("/business_metrics")
async def get_business_metrics_endpoint(_s: str = Depends(require_admin)):
    """获取业务级指标（LLM、记忆、主动消息等）。"""
    metrics = get_business_metrics()
    return JSONResponse(metrics.get_all_stats())


@metrics_router.get("/business_metrics/llm")
async def get_llm_metrics(_s: str = Depends(require_admin)):
    """获取 LLM 调用指标。"""
    metrics = get_business_metrics()
    return JSONResponse(metrics.get_llm_stats())


@metrics_router.get("/business_metrics/memory")
async def get_memory_metrics(_s: str = Depends(require_admin)):
    """获取记忆系统指标。"""
    metrics = get_business_metrics()
    return JSONResponse(metrics.get_memory_stats())


@metrics_router.get("/business_metrics/proactive")
async def get_proactive_metrics(_s: str = Depends(require_admin)):
    """获取主动消息指标。"""
    metrics = get_business_metrics()
    return JSONResponse(metrics.get_proactive_stats())
