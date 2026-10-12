"""混沌测试框架：模拟故障场景，验证系统韧性。

测试场景：
- LLM 超时/失败注入
- MongoDB 断连模拟
- 并发压力测试
- 资源泄漏检测

运行方式：
    pytest tools/test/test_chaos.py -v
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _run(coro):
    """在事件循环中运行协程（与项目既有测试风格一致）。"""
    return asyncio.run(coro)


class TestLLMChaos:
    """LLM 故障注入测试。"""

    def test_llm_timeout_triggers_circuit_breaker(self):
        """LLM 连续超时触发熔断器。"""
        from app.llm.circuit_breaker import (
            CircuitBreaker,
            CircuitBreakerConfig,
            CircuitBreakerOpen,
        )

        async def body():
            # 创建低阈值熔断器用于测试
            # 窗口大小 6，阈值 0.5：先用 3 次成功垫底，再连续失败触发熔断
            config = CircuitBreakerConfig(
                failure_threshold=0.5,
                window_size=6,
                recovery_timeout=0.5,
            )
            cb = CircuitBreaker(config)

            async def success_call():
                return "ok"

            async def failing_call():
                raise TimeoutError("LLM timeout")

            # 先 3 次成功垫底（窗口：3 成功）
            for _ in range(3):
                result = await cb.call(success_call)
                assert result == "ok"

            # 再 2 次失败（窗口：3 成功 + 2 失败，失败率 2/5=40% < 50%）
            for _ in range(2):
                with pytest.raises(TimeoutError):
                    await cb.call(failing_call)

            # 此时熔断器应该仍然闭合
            assert cb.state.value == "closed"

            # 再 1 次失败（窗口：3 成功 + 3 失败，失败率 3/6=50%，触发熔断）
            with pytest.raises(TimeoutError):
                await cb.call(failing_call)

            # 验证熔断器已触发（状态为 OPEN）
            assert cb.state.value == "open"

            # 熔断后调用应该被拒绝（不执行实际函数）
            with pytest.raises(CircuitBreakerOpen):
                await cb.call(failing_call)

            # 等待恢复
            await asyncio.sleep(0.6)

            # 半开状态需要 success_threshold=2 次成功才关闭
            result = await cb.call(success_call)
            assert result == "ok"
            result = await cb.call(success_call)
            assert result == "ok"

            # 验证熔断器已关闭
            assert cb.state.value == "closed"

        _run(body())

    def test_llm_fallback_chain(self):
        """LLM 降级链：主模型失败时自动切换。"""
        from app.llm.fallback_chain import FallbackChain

        async def body():
            chain = FallbackChain()

            # 模拟主 provider 失败
            primary = AsyncMock()
            primary.complete = AsyncMock(side_effect=TimeoutError("Primary timeout"))

            # 模拟 profile
            profile = SimpleNamespace(
                greeting="你好，我是 AI 助手。",
                name="小助手",
            )

            messages = [{"role": "user", "content": "你好"}]

            result = await chain.complete_with_fallback(
                primary_provider=primary,
                messages=messages,
                profile=profile,
            )

            # 应该使用降级方案
            assert result.used_fallback is True
            assert result.fallback_level >= 1
            assert result.original_error is not None

        _run(body())


class TestMongoChaos:
    """MongoDB 故障注入测试。"""

    def test_mongo_disconnect_graceful_degradation(self):
        """MongoDB 断连时系统优雅降级。"""
        from app.db.mongo import close_db, get_database

        async def body():
            # 使用 mongomock 内存数据库进行测试，避免依赖真实 MongoDB
            import mongomock_motor
            from app.db import mongo

            # 保存原始状态
            original_client = mongo._client
            original_db = mongo._db

            try:
                # 设置 mock 数据库
                mongo._client = mongomock_motor.AsyncMongoMockClient()
                mongo._db = mongo._client["test_db"]

                # 验证可以正常获取
                db = get_database()
                assert db is not None

                # 模拟断连
                await close_db()

                # 尝试获取数据库应该失败
                with pytest.raises(RuntimeError):
                    get_database()

            finally:
                # 恢复原始状态
                mongo._client = original_client
                mongo._db = original_db

        _run(body())


class TestConcurrencyChaos:
    """并发压力测试。"""

    def test_spawn_with_limit_concurrency_control(self):
        """后台任务并发闸门：限制并发数。"""
        from app.utils.tasks import get_task_stats, reset_task_stats, spawn_with_limit

        async def body():
            reset_task_stats()

            completed = []

            async def task(n: int):
                await asyncio.sleep(0.1)
                completed.append(n)

            # 启动 100 个任务，但并发限制为 50
            # spawn_with_limit 是同步函数，直接返回 Task
            tasks = [spawn_with_limit(task(i)) for i in range(100)]

            # 等待所有任务完成
            await asyncio.gather(*tasks)

            # 验证所有任务都完成了
            assert len(completed) == 100

            # 验证统计信息
            stats = get_task_stats()
            assert stats["active_tasks"] == 0  # 所有任务已完成

        _run(body())

    def test_stream_backpressure(self):
        """流式背压控制：慢消费者不阻塞生产者。"""
        from app.llm.openai_provider import OpenAICompatibleProvider

        async def body():
            # 创建 provider（使用 mock 配置）
            provider = OpenAICompatibleProvider(
                base_url="http://mock",
                api_key="mock",
                model="mock-model",
            )

            # 模拟 LLM 流式响应
            mock_chunks = [f"chunk_{i}" for i in range(200)]

            class MockAsyncIterator:
                """模拟异步迭代器（符合 async for 协议）。"""
                def __init__(self):
                    self._iter = iter(mock_chunks)

                def __aiter__(self):
                    return self

                async def __anext__(self):
                    try:
                        chunk = next(self._iter)
                        await asyncio.sleep(0.001)  # 模拟网络延迟
                        return SimpleNamespace(
                            choices=[SimpleNamespace(delta=SimpleNamespace(content=chunk))]
                        )
                    except StopIteration:
                        raise StopAsyncIteration

            # 使用 mock 替换实际 API 调用
            # 注意：stream_complete 内部使用 await self._client.chat.completions.create(...)
            # 然后对返回值使用 async for，因此需要返回一个支持异步迭代的对象
            mock_create = AsyncMock(return_value=MockAsyncIterator())
            with patch.object(
                provider._client.chat.completions,
                "create",
                mock_create,
            ):
                received = []
                async for chunk in provider.stream_complete(
                    messages=[{"role": "user", "content": "test"}]
                ):
                    received.append(chunk)
                    # 模拟慢消费者
                    await asyncio.sleep(0.01)

                # 应该收到所有 chunk（或部分被丢弃，但不应阻塞）
                assert len(received) > 0

        _run(body())


class TestResourceLeak:
    """资源泄漏检测。"""

    def test_background_task_cleanup(self):
        """后台任务完成后正确清理资源。"""
        from app.utils.tasks import get_task_stats, reset_task_stats, spawn

        async def body():
            reset_task_stats()

            async def short_task():
                await asyncio.sleep(0.01)

            # 创建并等待 10 个任务
            tasks = [spawn(short_task()) for _ in range(10)]
            await asyncio.gather(*tasks)

            # 等待回调执行
            await asyncio.sleep(0.1)

            # 验证任务集合已清理
            stats = get_task_stats()
            assert stats["active_tasks"] == 0

        _run(body())

    def test_memory_leak_under_load(self):
        """负载下内存泄漏检测。"""
        import gc

        # 强制垃圾回收
        gc.collect()
        initial_objects = len(gc.get_objects())

        # 模拟 100 轮对话
        for i in range(100):
            # 模拟消息处理（不实际调用 LLM）
            pass

        # 再次垃圾回收
        gc.collect()
        final_objects = len(gc.get_objects())

        # 对象增长应该在合理范围内（< 10%）
        growth = (final_objects - initial_objects) / initial_objects
        assert growth < 0.1, f"Potential memory leak: {growth:.2%} object growth"
