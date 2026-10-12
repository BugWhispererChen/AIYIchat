"""BUG 修复回归测试。

覆盖本次修复的关键逻辑（不依赖真实数据库/网络）：
- #1 list_available_profiles 的 account_id 可选
- #2 openai_provider 流式跳过空 choices
- #3 quota_repo.refund 返还额度
- #6 utils.tasks.spawn 持有任务引用
- #7 embedding_service 按 index 回填向量
- #10 asr_service 从 WAV 头解析采样率
"""

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch


class TestListAvailableProfilesSignature:
    """#1：list_available_profiles 的 account_id 应为可选参数（默认 None）。"""

    def test_account_id_optional(self) -> None:
        from app.conversation.engine import ConversationEngine

        sig = inspect.signature(ConversationEngine.list_available_profiles)
        assert sig.parameters["account_id"].default is None


class TestStreamCompleteEmptyChoices:
    """#2：流式生成应跳过 choices 为空的块，不抛 IndexError。"""

    def _make_provider(self):
        from app.llm.openai_provider import OpenAICompatibleProvider

        return OpenAICompatibleProvider(
            base_url="http://test", api_key="test", model="m", timeout=1
        )

    def test_skips_empty_choices(self) -> None:
        provider = self._make_provider()

        def delta(content):
            return SimpleNamespace(delta=SimpleNamespace(content=content))

        async def fake_stream():
            yield SimpleNamespace(choices=[])              # 空 choices → 跳过
            yield SimpleNamespace(choices=[delta("你好")])
            yield SimpleNamespace(choices=[delta(None)])   # None 内容 → 跳过
            yield SimpleNamespace(choices=[delta("世界")])
            yield SimpleNamespace(choices=[])              # 尾部 usage 块 → 跳过

        provider._client.chat.completions.create = AsyncMock(
            return_value=fake_stream()
        )

        async def _collect():
            return [d async for d in provider.stream_complete(messages=[])]

        result = asyncio.run(_collect())
        assert result == ["你好", "世界"]


class TestQuotaRefund:
    """#3：refund 以 $inc 负值原子返还额度。"""

    def test_refund_inc_negative(self) -> None:
        from app.db.repositories.quota_repo import QuotaRepository

        repo = QuotaRepository()
        fake_coll = MagicMock()
        fake_coll.update_one = AsyncMock(
            return_value=SimpleNamespace(modified_count=1)
        )
        repo._collection = lambda: fake_coll

        ok = asyncio.run(repo.refund("acc1", "text", 5))
        assert ok is True
        # update_one(filter, update)：第二个位置参数为更新文档
        update_doc = fake_coll.update_one.call_args[0][1]
        assert update_doc["$inc"]["text_quota.used"] == -5

    def test_refund_rejects_nonpositive(self) -> None:
        from app.db.repositories.quota_repo import QuotaRepository

        repo = QuotaRepository()
        fake_coll = MagicMock()
        fake_coll.update_one = AsyncMock()
        repo._collection = lambda: fake_coll

        assert asyncio.run(repo.refund("acc1", "text", 0)) is False
        fake_coll.update_one.assert_not_called()


class TestSpawnHoldsReference:
    """#6：spawn 应持有任务强引用，完成后自动清理。"""

    def test_reference_held_then_cleared(self) -> None:
        from app.utils.tasks import _background_tasks, spawn

        marker: list[int] = []

        async def _work():
            await asyncio.sleep(0)
            marker.append(1)

        async def _run():
            task = spawn(_work())
            assert task in _background_tasks  # 执行期间被强引用持有
            await task
            await asyncio.sleep(0)  # 让 done 回调执行
            return task

        task = asyncio.run(_run())
        assert marker == [1]
        assert task not in _background_tasks  # 完成后已移除


class TestEmbedBatchIndexMapping:
    """#7：embed_batch 应按返回项的 index 回填，抵御乱序/缺项。"""

    def _run_embed(self, texts, data_items):
        from app.services.embedding_service import EmbeddingService

        svc = EmbeddingService()
        fake_client = MagicMock()
        fake_client.embeddings.create = AsyncMock(
            return_value=SimpleNamespace(data=data_items)
        )
        svc._ensure_client = lambda: (fake_client, "model")

        with patch.object(
            EmbeddingService, "enabled", new_callable=PropertyMock
        ) as m_enabled:
            m_enabled.return_value = True
            return asyncio.run(svc.embed_batch(texts))

    def test_out_of_order_returns_are_realigned(self) -> None:
        # 返回顺序与输入相反，按 index 回填后应对齐原始位置
        data = [
            SimpleNamespace(index=1, embedding=[0.2]),
            SimpleNamespace(index=0, embedding=[0.1]),
        ]
        result = self._run_embed(["a", "b"], data)
        assert result == [[0.1], [0.2]]

    def test_missing_item_stays_empty(self) -> None:
        # 仅返回一项时，缺失项保持空向量而非错位
        data = [SimpleNamespace(index=0, embedding=[0.9])]
        result = self._run_embed(["a", "b"], data)
        assert result == [[0.9], []]


class TestWavSampleRate:
    """#10：从 WAV 头解析采样率，非 WAV 数据回退默认值。"""

    def test_parses_declared_rate(self) -> None:
        from app.services.asr_service import _wav_sample_rate
        from app.services.audio_utils import _pcm_to_wav

        wav24 = _pcm_to_wav(b"\x00\x00" * 100, sample_rate=24000)
        wav16 = _pcm_to_wav(b"\x00\x00" * 100, sample_rate=16000)
        assert _wav_sample_rate(wav24) == 24000
        assert _wav_sample_rate(wav16) == 16000

    def test_non_wav_returns_default(self) -> None:
        from app.services.asr_service import _wav_sample_rate

        assert _wav_sample_rate(b"not-a-wav-header", default=16000) == 16000
