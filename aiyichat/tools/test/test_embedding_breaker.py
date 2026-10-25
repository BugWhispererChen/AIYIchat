"""Embedding 连续失败熔断测试。

背景：测试服 DashScope 免费额度耗尽后，日志持续 `批量/单条 Embedding 失败: 400
AllocationQuota.FreeTierOnly`，而 DB 里 `embedding_enabled=true` ——
`EmbeddingService.enabled` 只看开关与密钥、不看失败历史，于是每条消息都白打一次 HTTP，
失败计数只增不减，语义检索长期静默降级。本文件锁死熔断的三个行为：
连续失败即停发请求、冷却后自动试探恢复、熔断本身不再计为新失败。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.services.embedding_service import EmbeddingService


def _run(coro) -> None:
    asyncio.run(coro)


@pytest.fixture(autouse=True)
def _breaker_switch_on():
    """熔断开关显式钉成开启：测试不依赖同进程里别的用例留下的 settings 残留值。"""
    with patch.object(settings, "embedding_breaker_enabled", True):
        yield


def _make_service(fail_times: int | None = None) -> EmbeddingService:
    """构造一个"看起来已配好密钥"的服务实例，create 由外部替身驱动。

    fail_times=None 表示成功；传 Exception 工厂时按次抛错。
    """
    svc = EmbeddingService()
    svc._model = "emb-m"
    svc._client = SimpleNamespace(embeddings=SimpleNamespace(create=AsyncMock()))
    return svc


def _fail_response(exc: Exception) -> AsyncMock:
    return AsyncMock(side_effect=exc)


def _ok_item() -> SimpleNamespace:
    return SimpleNamespace(data=[SimpleNamespace(embedding=[0.5], index=0)])


class TestBreakerTrips:
    """连续失败达窗口阈值后停止发出请求。"""

    def test_repeated_failures_open_the_breaker(self) -> None:
        async def body():
            svc = _make_service()
            quota_error = RuntimeError("400 AllocationQuota.FreeTierOnly")
            svc._client.embeddings.create = _fail_response(quota_error)
            with patch.object(type(svc), "enabled", property(lambda self: True)):
                # 窗口 10、阈值 0.6、最少 5 个样本 → 第 5 次失败即熔断
                for _ in range(8):
                    assert await svc.embed("文本") == []
            assert svc._breaker.state.value == "open"
            # 熔断后不再白打 HTTP：调用次数停在触发熔断的那 5 次
            assert svc._client.embeddings.create.await_count == 5

        _run(body())

    def test_breaker_open_is_not_counted_as_new_failure(self) -> None:
        async def body():
            svc = _make_service()
            svc._client.embeddings.create = _fail_response(RuntimeError("额度耗尽"))
            with patch.object(type(svc), "enabled", property(lambda self: True)):
                for _ in range(10):
                    await svc.embed("文本")
            # 失败计数只到真正发过请求的那 5 次，其余是熔断挡下的
            assert svc._fail_count == 5
            status = svc.status()
            assert status["breaker_state"] == "open"
            assert status["recovery_in_seconds"] > 0

        _run(body())

    def test_batch_keeps_partial_results_when_open(self) -> None:
        """批量路径在熔断中返回等长空向量，调用方的规则排序降级不受影响。"""
        async def body():
            svc = _make_service()
            svc._client.embeddings.create = _fail_response(RuntimeError("额度耗尽"))
            with patch.object(type(svc), "enabled", property(lambda self: True)):
                for _ in range(6):
                    await svc.embed_batch(["a"])
            out = await svc.embed_batch(["a", "b", "c"])
            assert out == [[], [], []]
            assert svc._client.embeddings.create.await_count == 5

        _run(body())


class TestBreakerRecovery:
    """冷却后半开试探：成功即恢复，失败则重新熔断。"""

    def test_probe_success_closes_breaker(self) -> None:
        async def body():
            svc = _make_service()
            svc._client.embeddings.create = _fail_response(RuntimeError("额度耗尽"))
            with patch.object(type(svc), "enabled", property(lambda self: True)):
                for _ in range(6):
                    await svc.embed("文本")
            assert svc._breaker.state.value == "open"

            # 冷却到期（把 recovery_timeout 设为 0 模拟等待完成），并让端点恢复
            svc._breaker.config.recovery_timeout = 0.0
            svc._client.embeddings.create = AsyncMock(return_value=_ok_item())
            with patch.object(type(svc), "enabled", property(lambda self: True)):
                assert await svc.embed("文本") == [0.5]
            assert svc._breaker.state.value == "closed"

        _run(body())

    def test_probe_failure_reopens_breaker(self) -> None:
        async def body():
            svc = _make_service()
            svc._client.embeddings.create = _fail_response(RuntimeError("额度耗尽"))
            with patch.object(type(svc), "enabled", property(lambda self: True)):
                for _ in range(6):
                    await svc.embed("文本")
            calls_after_open = svc._client.embeddings.create.await_count

            svc._breaker.config.recovery_timeout = 0.0
            with patch.object(type(svc), "enabled", property(lambda self: True)):
                # 半开只放行一次试探，试探仍失败 → 重新 OPEN
                assert await svc.embed("文本") == []
            svc._breaker.config.recovery_timeout = 600.0
            with patch.object(type(svc), "enabled", property(lambda self: True)):
                # 重回 OPEN 后立即恢复真实冷却，后续调用被直接挡下、不再打 HTTP
                assert await svc.embed("文本") == []
            assert svc._breaker.state.value == "open"
            assert (
                svc._client.embeddings.create.await_count
                == calls_after_open + 1
            )

        _run(body())


class TestBreakerSwitch:
    """开关关闭即回退旧行为：失败也照打，不熔断。"""

    def test_disabled_switch_keeps_calling_endpoint(self) -> None:
        async def body():
            svc = _make_service()
            svc._client.embeddings.create = _fail_response(RuntimeError("额度耗尽"))
            with patch.object(type(svc), "enabled", property(lambda self: True)), patch.object(
                settings, "embedding_breaker_enabled", False
            ):
                for _ in range(8):
                    assert await svc.embed("文本") == []
            assert svc._client.embeddings.create.await_count == 8
            assert svc._fail_count == 8
            assert svc._breaker.state.value == "closed"

        _run(body())

    def test_status_exposes_breaker_state(self) -> None:
        async def body():
            svc = _make_service()
            status = svc.status()
            assert status["breaker_state"] == "closed"
            assert status["recovery_in_seconds"] == 0

        _run(body())


class TestErrorCodeFingerprint:
    """错误码单独暴露：仪表盘的 last_error 截 120 字符，额度耗尽只能靠错误码辨认。"""

    def _api_error(self, body: object, status_code: int = 400):
        import httpx

        from openai import APIStatusError

        # openai 的异常会读取 response.request，裸构造的 Response 没有它，需显式挂上
        response = httpx.Response(
            status_code,
            request=httpx.Request("POST", "https://dashscope/compatible/v1/embeddings"),
        )
        return APIStatusError(
            f"Error code: {status_code} - {body!r} " + "x" * 500,
            response=response,
            body=body,
        )

    def test_flat_code_is_extracted(self) -> None:
        err = self._api_error({"code": "AllocationQuota.FreeTierOnly", "message": "m"})
        assert EmbeddingService._error_fingerprint(err) == (
            "400:AllocationQuota.FreeTierOnly"
        )

    def test_nested_error_code_is_extracted(self) -> None:
        err = self._api_error(
            {"error": {"code": "Throttling", "message": "m"}}, status_code=429
        )
        assert EmbeddingService._error_fingerprint(err) == "429:Throttling"

    def test_non_openai_error_falls_back_to_text(self) -> None:
        assert EmbeddingService._error_fingerprint(
            RuntimeError("400 AllocationQuota.FreeTierOnly")
        ) == "400 AllocationQuota.FreeTierOnly"

    def test_record_failure_populates_code_without_touching_last_error(self) -> None:
        """`last_error` 保持原样（模板已有截断展示），错误码是新增的独立字段。"""
        svc = _make_service()
        long_err = self._api_error({"code": "AllocationQuota.FreeTierOnly"})
        svc._record_failure(long_err)
        status = svc.status()
        assert status["last_error_code"] == "400:AllocationQuota.FreeTierOnly"
        assert status["last_error"] == str(long_err)
        assert status["fail_count"] == 1


class TestSlowSuccessCounting:
    """慢成功计入熔断：端点不报错但每次挂几十秒时，也必须能被熔断挡下。

    2026-10 测试服实测 embedding 端点偶发单次请求 80s+ 仍返回 200，
    只按异常计数的熔断器永远抓不到这种拖垮整轮回复的情况。
    """

    def test_slow_successes_open_the_breaker_but_still_return(self) -> None:
        async def body():
            svc = _make_service()

            async def slow_create(*_args, **_kwargs):
                await asyncio.sleep(0.02)
                return _ok_item()

            svc._client.embeddings.create = slow_create
            svc._breaker.config.slow_failure_seconds = 0.005
            with patch.object(type(svc), "enabled", property(lambda self: True)):
                vectors = [await svc.embed("文本") for _ in range(6)]
            # 结果照常返回（慢≠错），但连续慢已把熔断推开
            assert vectors[0] == [0.5]
            assert svc._breaker.state.value == "open"

        _run(body())

    def test_fast_success_does_not_count_as_failure(self) -> None:
        async def body():
            svc = _make_service()
            svc._client.embeddings.create = AsyncMock(return_value=_ok_item())
            svc._breaker.config.slow_failure_seconds = 5.0
            with patch.object(type(svc), "enabled", property(lambda self: True)):
                for _ in range(8):
                    assert await svc.embed("文本") == [0.5]
            assert svc._breaker.state.value == "closed"
            assert svc._breaker.failure_rate == 0.0

        _run(body())


class TestClientBudget:
    """客户端超时/重试口径与 chat provider 一致：慢就快速失败，不靠 SDK 默认重试。"""

    def test_timeout_and_zero_retries(self) -> None:
        from app.llm.model_registry import ResolvedModelConfig

        svc = EmbeddingService()
        with patch(
            "app.services.embedding_service.resolve_model_config",
            return_value=ResolvedModelConfig(
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                api_key="sk-test", model="emb-m", timeout=30,
            ),
        ):
            client, model = svc._ensure_client()
        assert model == "emb-m"
        assert client.timeout == 8.0
        assert client.max_retries == 0


class TestWithinBudgetHelpers:
    """检索类向量化统一走预算：超时即空向量，调用方按既有逻辑降级。"""

    class _Slow:
        enabled = True

        async def embed(self, text: str) -> list[float]:
            await asyncio.sleep(0.05)
            return [1.0]

        async def embed_batch(self, texts: list[str]) -> list[list[float]]:
            await asyncio.sleep(0.05)
            return [[1.0] for _ in texts]

    class _Off:
        enabled = False

        async def embed(self, text: str) -> list[float]:  # pragma: no cover
            raise AssertionError("未启用不应被调用")

        async def embed_batch(self, texts):  # pragma: no cover
            raise AssertionError("未启用不应被调用")

    def test_embed_timeout_returns_empty_and_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from app.services.embedding_service import embed_within_budget

        async def body():
            with caplog.at_level("WARNING"), patch.object(
                settings, "memory_recall_timeout_seconds", 0.01
            ):
                assert await embed_within_budget(
                    TestWithinBudgetHelpers._Slow(), "文本", what="记忆语义召回"
                ) == []
        _run(body())
        assert "记忆语义召回" in caplog.text

    def test_batch_timeout_returns_equal_length_empty(self) -> None:
        from app.services.embedding_service import embed_batch_within_budget

        async def body():
            with patch.object(settings, "memory_recall_timeout_seconds", 0.01):
                out = await embed_batch_within_budget(
                    TestWithinBudgetHelpers._Slow(), ["a", "b", "c"], what="向量回填"
                )
            assert out == [[], [], []]

        _run(body())

    def test_disabled_service_short_circuits(self) -> None:
        from app.services.embedding_service import (
            embed_batch_within_budget,
            embed_within_budget,
        )

        async def body():
            off = TestWithinBudgetHelpers._Off()
            assert await embed_within_budget(off, "文本", what="x") == []
            assert await embed_batch_within_budget(off, ["a", "b"], what="x") == [[], []]

        _run(body())
