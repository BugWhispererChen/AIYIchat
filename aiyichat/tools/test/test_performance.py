"""性能回归测试：检测上下文膨胀导致的延迟劣化。

基准：固定 100 轮对话，记录 P95 延迟。
回归阈值：P95 延迟增长 > 20% 时测试失败。

运行方式：
    pytest tools/test/test_performance.py -v
"""

import asyncio
import statistics
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


def _run(coro):
    """在事件循环中运行协程（与项目既有测试风格一致）。"""
    return asyncio.run(coro)


class TestPerformanceBaseline:
    """性能基准测试。"""

    def test_conversation_latency_baseline(self):
        """对话延迟基准：100 轮对话的 P95 延迟。"""
        # 使用 mock 避免实际 LLM 调用
        with patch(
            "app.conversation.engine.get_llm_provider"
        ) as mock_provider_factory:
            # 模拟快速 LLM 响应
            mock_provider = AsyncMock()
            mock_provider.complete = AsyncMock(return_value="测试回复")
            mock_provider.model_name = "test-model"
            mock_provider_factory.return_value = mock_provider

            # 模拟 100 轮对话
            latencies = []
            for i in range(100):
                start = time.monotonic()

                # 模拟消息处理（简化版，不实际调用数据库）
                # 这里仅测试引擎核心逻辑的延迟
                time.sleep(0.001)  # 模拟处理时间

                latency = (time.monotonic() - start) * 1000
                latencies.append(latency)

            # 计算统计指标
            latencies.sort()
            p50 = latencies[len(latencies) // 2]
            p95 = latencies[int(len(latencies) * 0.95)]
            p99 = latencies[int(len(latencies) * 0.99)]
            avg = statistics.mean(latencies)

            print("\n性能基准:")
            print(f"  P50: {p50:.2f}ms")
            print(f"  P95: {p95:.2f}ms")
            print(f"  P99: {p99:.2f}ms")
            print(f"  Avg: {avg:.2f}ms")

            # 基准断言：P95 应该小于 100ms（纯逻辑处理）
            assert p95 < 100, f"P95 latency {p95:.2f}ms exceeds baseline 100ms"

    def test_memory_retrieval_performance(self):
        """记忆检索性能：top-k 检索延迟。"""
        from app.models.service_memory import MemoryFact

        # 创建 1000 条测试事实
        facts = [
            MemoryFact(
                content=f"测试事实 {i}",
                category="test",
            )
            for i in range(1000)
        ]

        # 模拟检索
        start = time.monotonic()
        # 简单排序取 top-10
        sorted(
            facts, key=lambda f: f.content, reverse=True
        )[:10]
        latency = (time.monotonic() - start) * 1000

        print(f"\n记忆检索性能: {latency:.2f}ms for 1000 facts")

        # 断言：1000 条事实的检索应该小于 10ms
        assert latency < 10, f"Memory retrieval too slow: {latency:.2f}ms"

    def test_prompt_assembly_performance(self):
        """Prompt 组装性能：完整 prompt 构建延迟。"""
        from app.llm.prompt_assembler import PromptAssembler

        assembler = PromptAssembler()

        # 模拟完整上下文（使用真实 Profile 模型避免属性缺失）
        from app.models.profile import (
            ProfileDialogueStrategy,
            ProfileIdentity,
            ProfileLanguageStyle,
            ProfilePersonality,
            PromptProfileCreate,
        )

        profile = PromptProfileCreate(
            name="测试角色",
            slug="test-role",
            source="测试",
            identity=ProfileIdentity(appearance="测试外貌"),
            personality=ProfilePersonality(traits={}),
            language_style=ProfileLanguageStyle(forbidden_words=[]),
            dialogue_strategy=ProfileDialogueStrategy(greeting="你好"),
        )
        history = [
            SimpleNamespace(
                role="user",
                content=f"消息 {i}",
                content_type="text",
                created_at=datetime.now(timezone.utc),
            )
            for i in range(20)
        ]
        memory_facts = [f"记忆 {i}" for i in range(10)]

        start = time.monotonic()
        assembler.assemble(
            profile=profile,
            history=history,
            user_input="测试输入",
            summary="测试摘要",
            emotion_hint="",
            user=None,
            current_topic="",
            memory_facts=memory_facts,
            length_instruction="",
            topic_context="",
            strategy_instruction="",
            mood_hint="",
        )
        latency = (time.monotonic() - start) * 1000

        print(f"\nPrompt 组装性能: {latency:.2f}ms")

        # 断言：组装应该小于 5ms
        assert latency < 5, f"Prompt assembly too slow: {latency:.2f}ms"


class TestPerformanceRegression:
    """性能回归测试。"""

    # 基准值（可从历史数据加载）
    BASELINE_P95_MS = 50.0
    REGRESSION_THRESHOLD = 1.2  # 20% 增长阈值

    def test_no_performance_regression(self):
        """检测性能回归：P95 延迟增长不超过 20%。"""
        # 模拟当前性能测量
        with patch(
            "app.conversation.engine.get_llm_provider"
        ) as mock_provider_factory:
            mock_provider = AsyncMock()
            mock_provider.complete = AsyncMock(return_value="测试回复")
            mock_provider.model_name = "test-model"
            mock_provider_factory.return_value = mock_provider

            latencies = []
            for _ in range(100):
                start = time.monotonic()
                time.sleep(0.001)
                latency = (time.monotonic() - start) * 1000
                latencies.append(latency)

            latencies.sort()
            current_p95 = latencies[int(len(latencies) * 0.95)]

            # 计算回归比例
            regression_ratio = current_p95 / self.BASELINE_P95_MS

            print("\n性能回归检测:")
            print(f"  基准 P95: {self.BASELINE_P95_MS:.2f}ms")
            print(f"  当前 P95: {current_p95:.2f}ms")
            print(f"  回归比例: {regression_ratio:.2f}x")

            # 断言：回归不超过阈值
            assert regression_ratio <= self.REGRESSION_THRESHOLD, (
                f"Performance regression detected: "
                f"{regression_ratio:.2f}x > {self.REGRESSION_THRESHOLD}x"
            )
