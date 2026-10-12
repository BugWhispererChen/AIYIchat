"""后台任务工具：安全地 fire-and-forget，防止任务被 GC 提前取消。

asyncio 事件循环仅对 Task 持有弱引用，未保存返回值的 create_task 结果可能在
执行过程中被垃圾回收取消（静默失败）。此处用模块级集合持有强引用，任务完成后
通过 done 回调自动移除，避免集合无限增长。

新增 spawn_with_limit：全局信号量控制并发后台任务数，防止高并发场景下事件循环过载。
"""

import asyncio
import logging
from typing import Any, Coroutine

logger = logging.getLogger(__name__)

# 持有后台任务的强引用，防止其在完成前被 GC 回收
_background_tasks: set[asyncio.Task] = set()

# 全局并发闸门：限制同时运行的后台任务数（默认 50）
_global_semaphore: asyncio.Semaphore | None = None
_DEFAULT_CONCURRENCY_LIMIT = 50

# 等待队列统计（用于监控）
_pending_tasks_count = 0


def _get_semaphore() -> asyncio.Semaphore:
    """获取或创建全局信号量。"""
    global _global_semaphore
    if _global_semaphore is None:
        _global_semaphore = asyncio.Semaphore(_DEFAULT_CONCURRENCY_LIMIT)
    return _global_semaphore


def _on_task_done(task: asyncio.Task) -> None:
    """任务完成回调：移除引用并记录未处理异常（便于排查静默失败）。"""
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("后台任务异常: %s", exc)


def _on_limited_task_done(task: asyncio.Task) -> None:
    """带并发限制的任务完成回调：释放信号量并记录异常。"""
    global _pending_tasks_count
    _on_task_done(task)
    _get_semaphore().release()
    _pending_tasks_count = max(0, _pending_tasks_count - 1)


def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
    """创建后台任务并持有强引用，避免被 GC 提前取消。

    用于替代裸 asyncio.create_task 的 fire-and-forget 场景。
    """
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_on_task_done)
    return task


def spawn_with_limit(
    coro: Coroutine[Any, Any, Any],
    *,
    timeout: float | None = None,
) -> asyncio.Task:
    """创建带并发限制的后台任务（同步包装，与 spawn 签名一致）。

    使用全局信号量控制并发数，超限任务进入等待队列。
    适用于高并发场景下的资源保护，避免事件循环过载。

    注意：此函数为同步包装，内部通过 asyncio.create_task 启动一个
    包装协程来异步获取信号量，因此可以在同步上下文中直接调用。

    Args:
        coro: 要执行的协程
        timeout: 等待信号量的超时时间（秒），None 表示无限等待

    Returns:
        创建的 asyncio.Task 对象
    """
    global _pending_tasks_count

    async def _wrapped() -> Any:
        """包装协程：先获取信号量，再执行实际任务。"""
        global _pending_tasks_count
        semaphore = _get_semaphore()

        # 等待获取信号量（带可选超时）
        if timeout is not None:
            await asyncio.wait_for(semaphore.acquire(), timeout=timeout)
        else:
            await semaphore.acquire()

        _pending_tasks_count += 1
        try:
            return await coro
        finally:
            semaphore.release()
            _pending_tasks_count = max(0, _pending_tasks_count - 1)

    # 创建任务并注册回调
    task = asyncio.create_task(_wrapped())
    _background_tasks.add(task)
    task.add_done_callback(_on_task_done)
    return task


def get_task_stats() -> dict[str, int]:
    """获取后台任务统计信息（用于监控）。"""
    semaphore = _get_semaphore()
    return {
        "active_tasks": len(_background_tasks),
        "pending_tasks": _pending_tasks_count,
        "concurrency_limit": _DEFAULT_CONCURRENCY_LIMIT,
        "available_slots": semaphore._value if hasattr(semaphore, "_value") else 0,
    }


def reset_task_stats() -> None:
    """重置任务统计（用于测试）。"""
    global _global_semaphore, _pending_tasks_count
    _background_tasks.clear()
    _global_semaphore = None
    _pending_tasks_count = 0
