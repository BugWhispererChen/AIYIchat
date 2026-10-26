"""克隆评测器、P0 契约修复与 C 端反馈闭环测试。"""

import asyncio
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import mongomock_motor
from httpx import ASGITransport, AsyncClient

from app.chat_import.models import ChatMessage
from app.db import mongo
from app.models.clone import CloneFeedbackInDB


def _run(coro):
    return asyncio.run(coro)


def _fresh_db(name: str) -> None:
    mongo._db = mongomock_motor.AsyncMongoMockClient()[name]


def _load_eval_module():
    path = Path(__file__).resolve().parents[1] / "eval" / "run_clone_eval.py"
    spec = importlib.util.spec_from_file_location("run_clone_eval", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestP0Contracts:
    def test_identity_is_mapped_to_persona(self) -> None:
        from app.chat_import.analyzer import _ensure_profile_structure

        out = _ensure_profile_structure({
            "identity": {
                "background": "杭州上班族",
                "appearance": "short hair",
                "relationships": [{"name": "阿青", "relation": "朋友"}],
            },
            "personality": {"traits": {"warmth": "0.8", "bad": "高"}},
        })
        assert "identity" not in out
        assert out["persona"]["background"] == "杭州上班族"
        assert out["persona"]["appearance"] == "short hair"
        assert len(out["persona"]["relationships"]) == 1
        assert out["personality"]["traits"] == {"warmth": 0.8}

    def test_merge_batches_respect_input_budget(self) -> None:
        from app.chat_import.analyzer import _pack_merge_batches

        results = [
            {"language_style": {"tone": "x" * 9000}},
            {"language_style": {"tone": "y" * 9000}},
            {"language_style": {"tone": "z" * 100}},
        ]
        batches = _pack_merge_batches(results, max_chars=12000)
        assert [len(batch) for batch in batches] == [1, 2]

    def test_merge_analyses_reduces_in_rounds(self, monkeypatch) -> None:
        async def body():
            from app.chat_import import analyzer

            monkeypatch.setattr(analyzer, "_MERGE_INPUT_CHARS", 1500)
            profile_json = json.dumps({
                "persona": {"background": "有证据的背景"},
                "language_style": {"tone": "简短"},
            })
            provider = SimpleNamespace(complete=AsyncMock(return_value=profile_json))
            results = [
                {"chunk": idx, "detail": "x" * 600} for idx in range(3)
            ]
            merged = await analyzer._merge_analyses(
                provider, results, "张三", "小张", ""
            )
            assert merged["persona"]["background"] == "有证据的背景"
            # 3 个分块先分成 2 批归并，再最终归并一次
            assert provider.complete.await_count == 3

        _run(body())

    def test_partial_chunk_failure_is_not_reported_done(self, monkeypatch) -> None:
        async def body():
            _fresh_db("test_partial_chunks")
            from app.chat_import import analyzer

            analyzer._tasks.clear()
            await analyzer.create_task("t-partial", "/tmp")
            messages = [
                ChatMessage(
                    timestamp=datetime(2024, 3, 15, 14, i, tzinfo=timezone.utc),
                    sender="张三", content=f"我今天很开心{i}", is_target=True,
                    source_index=i,
                )
                for i in range(2)
            ]
            monkeypatch.setattr(
                "app.config.settings.chat_import_chunk_size", 1
            )
            fake = SimpleNamespace(complete=AsyncMock(
                side_effect=[
                    json.dumps({"tone": "温和"}),
                    "not-json", "not-json", "not-json",
                ]
            ))
            try:
                with patch(
                    "app.chat_import.analyzer.get_llm_provider",
                    return_value=fake,
                ):
                    await analyzer.run_analysis("t-partial", messages)
                task = await analyzer.get_task("t-partial")
                assert task is not None
                assert task.status == "error"
                assert task.chunk_total == 2
                assert task.chunk_succeeded == 1
                assert task.failed_chunk_indexes == [1]
                assert "分块分析不完整" in task.error_message
            finally:
                analyzer._tasks.clear()
                mongo._db = None

        _run(body())


class TestCloneEvalHarness:
    def test_metrics_and_bootstrap_are_deterministic(self) -> None:
        module = _load_eval_module()
        metrics = module._surface_metrics("好家伙😂", "好家伙，这也太晚了吧😂")
        assert metrics["reply_chars"] == 4
        assert metrics["char_gap"] > 0
        assert module._leaked_spans("我知道我妈住院了", ["我妈住院"]) == ["我妈住院"]
        assert module._bootstrap_ci([1, 1, -1, 0, 1]) == module._bootstrap_ci(
            [1, 1, -1, 0, 1]
        )

    def test_example_fixture_dry_run_reaches_dynamic_hint(self) -> None:
        module = _load_eval_module()
        root = Path(__file__).resolve().parents[1] / "eval"
        doc = json.loads((root / "clone_cases.example.json").read_text("utf-8"))
        result = _run(module.run(doc, dry_run=True, allow_non_test=False))
        assert result["summary"] == {"cases": 1}
        record = result["cases"][0]
        assert record["retrieved"]
        assert "相似情境下的表达参考" in record["clone_hint"]
        assert record["clone_prompt_tokens"] > record["baseline_prompt_tokens"]

    def test_non_test_split_is_rejected(self) -> None:
        import pytest

        module = _load_eval_module()
        doc = {"roles": [{
            "name": "泄漏角色",
            "train_messages": [],
            "test_cases": [{"name": "train", "split": "train", "user_input": "x"}],
        }]}
        with pytest.raises(ValueError, match="非 test"):
            _run(module.run(doc, dry_run=True, allow_non_test=False))


class TestCloneFeedback:
    def test_feedback_endpoint_records_latest_reply(self) -> None:
        async def body():
            _fresh_db("test_clone_feedback")
            from app.api import web_clone_api
            from app.db.repositories.clone_repo import CloneFeedbackRepository
            from app.db.repositories.profile_repo import ProfileRepository
            from app.models.profile import PromptProfileCreate

            profile = await ProfileRepository().create(PromptProfileCreate(
                slug="clone-feedback", name="可反馈角色",
                visibility="private", account_id="acc-1",
                clone_status="active", clone_enabled=True,
                clone_dataset_id="d1", clone_version=3,
            ))
            principal = SimpleNamespace(account_id="acc-1", identity_id="id-1")
            service = SimpleNamespace(
                id="s1", profile_id=profile.id, share_group_id="", identity_id="id-1",
            )
            conv = SimpleNamespace(
                id="c1", service_id="s1",
                last_message_at=datetime.now(timezone.utc),
            )
            conv_repo = SimpleNamespace(find_by_id=AsyncMock(return_value=conv))
            msg_repo = SimpleNamespace(get_recent=AsyncMock(return_value=[
                SimpleNamespace(role="user", id="m-user"),
                SimpleNamespace(role="assistant", id="m-ai"),
            ]))
            try:
                with patch.object(
                    web_clone_api, "require_current_identity",
                    AsyncMock(return_value=principal),
                ), patch.object(
                    web_clone_api, "require_account_service",
                    AsyncMock(return_value=service),
                ), patch.object(
                    web_clone_api, "ConversationRepository", return_value=conv_repo,
                ), patch.object(
                    web_clone_api, "MessageRepository", return_value=msg_repo,
                ):
                    response = await web_clone_api.submit_clone_feedback(
                        None, web_clone_api.CloneFeedbackPayload(
                            service_id="s1", conversation_id="c1",
                            rating="dislike", reasons=["tone", "injected"],
                            edited_reply="短一点",
                        )
                    )
                assert response.status_code == 201
                items = await CloneFeedbackRepository().list_by_profile(profile.id)
                assert len(items) == 1
                assert items[0].message_id == "m-ai"
                assert items[0].clone_version == 3
                assert items[0].reasons == ["tone"]
            finally:
                mongo._db = None

        _run(body())

    def test_admin_feedback_page_and_status(self) -> None:
        async def body():
            _fresh_db("test_clone_feedback_admin")
            from app.admin.auth import require_admin
            from app.db.repositories.clone_repo import CloneFeedbackRepository
            from app.main import app

            app.dependency_overrides[require_admin] = lambda: "admin"
            feedback = await CloneFeedbackRepository().create(CloneFeedbackInDB(
                account_id="acc-1", service_id="s1",
                profile_id="p1", clone_version=1, rating="dislike",
            ))
            try:
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    page = await client.get("/admin/clone-feedback")
                    assert page.status_code == 200
                    assert "不像" in page.text
                    response = await client.post(
                        f"/admin/clone-feedback/{feedback.id}/status",
                        data={"status": "reviewed"},
                    )
                    assert response.status_code == 303
                items = await CloneFeedbackRepository().list_by_profile("p1")
                assert items[0].status == "reviewed"
            finally:
                app.dependency_overrides.clear()
                mongo._db = None

        _run(body())
