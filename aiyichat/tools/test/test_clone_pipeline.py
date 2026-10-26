"""聊天记录克隆：片段构建、风格统计、检索隔离与入库激活。"""

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import patch

import mongomock_motor
from httpx import ASGITransport, AsyncClient

from app.chat_import.clone_corpus import (
    build_episodes,
    compute_style_profile,
    merge_role_memories,
    render_style_hint,
    select_runtime_episodes,
)
from app.chat_import.models import AnalysisTask, ChatMessage
from app.config import settings
from app.db import mongo
from app.models.clone import CloneDatasetInDB, CloneEpisodeInDB


def _run(coro):
    return asyncio.run(coro)


def _fresh_db(name: str = "test_clone") -> None:
    mongo._db = mongomock_motor.AsyncMongoMockClient()[name]


def _msg(
    minute: int,
    sender: str,
    content: str,
    *,
    target: bool,
    index: int,
    media: bool = False,
) -> ChatMessage:
    return ChatMessage(
        timestamp=datetime(2024, 3, 15, 14, minute, tzinfo=timezone.utc),
        sender=sender,
        content=content,
        is_target=target,
        source_index=index,
        message_type="media" if media else "text",
    )


class TestCloneCorpus:
    def test_build_episodes_groups_bubbles_and_short_replies(self) -> None:
        messages = [
            _msg(0, "我", "今天加班到十点，好累", target=False, index=0),
            _msg(1, "张三", "啊这也太晚了", target=True, index=1),
            _msg(2, "张三", "快回去歇着", target=True, index=2),
            _msg(20, "我", "我升职了！！！", target=False, index=3),
            _msg(21, "张三", "真的假的！", target=True, index=4),
            _msg(22, "张三", "[表情]", target=True, index=5, media=True),
        ]
        episodes = build_episodes(messages, "张三")
        assert len(episodes) == 2
        assert episodes[0].target_text == "啊这也太晚了|||快回去歇着"
        assert episodes[0].scene == "comfort"
        assert episodes[1].scene == "celebrate"
        assert episodes[0].source_indexes == [0, 1, 2]

    def test_style_profile_counts_short_and_media(self) -> None:
        messages = [
            _msg(0, "张三", "嗯", target=True, index=0),
            _msg(1, "张三", "[语音]", target=True, index=1, media=True),
            _msg(2, "我", "在吗", target=False, index=2),
            _msg(3, "张三", "在呀", target=True, index=3),
        ]
        episodes = build_episodes(messages, "张三")
        style = compute_style_profile(messages, episodes)
        assert style.sample_count == 3
        assert style.text_count == 2
        assert style.media_count == 1
        assert style.overall.short_rate == 1.0
        assert style.overall.median_chars == 2
        assert "呀" in style.common_particles
        assert "短回复占比" in render_style_hint(style)

    def test_runtime_selection_deduplicates(self) -> None:
        messages = [
            _msg(0, "我", "在吗", target=False, index=0),
            _msg(1, "张三", "在呀", target=True, index=1),
            _msg(2, "我", "在吗", target=False, index=2),
            _msg(3, "张三", "在呀", target=True, index=3),
        ]
        episodes = build_episodes(messages, "张三")
        assert len(episodes) == 2
        assert len(select_runtime_episodes(episodes)) == 1

    def test_merge_role_memories_dedupes_with_evidence(self) -> None:
        memories = merge_role_memories([
            {"role_facts": [
                {"content": "我今年刚搬到杭州", "kind": "life_event", "confidence": 0.9},
                {"content": "嗯", "confidence": 1},
            ]},
            {"role_facts": [
                {"content": "我今年刚搬到杭州", "kind": "life_event", "confidence": 0.8},
            ]},
        ])
        assert len(memories) == 1
        assert memories[0].source_chunk_indexes == [0]
        assert memories[0].confidence == 0.9


class TestCloneRetrieval:
    def test_retrieves_only_active_matching_version_and_account(self) -> None:
        async def body():
            _fresh_db("test_clone_retrieval")
            from app.db.repositories.clone_repo import (
                CloneDatasetRepository,
                CloneEpisodeRepository,
            )
            from app.models.profile import PromptProfileInDB
            from app.services.clone_service import (
                render_clone_context,
                retrieve_clone_context,
            )

            old_flag = settings.clone_retrieval_enabled
            settings.clone_retrieval_enabled = True
            try:
                dataset = CloneDatasetInDB(
                    account_id="acc-1",
                    profile_id="p1",
                    version=1,
                    status="active",
                    role_memories=[],
                )
                await CloneDatasetRepository().create(dataset)
                await CloneEpisodeRepository().create_many([
                    CloneEpisodeInDB(
                        dataset_id=dataset.id, account_id="acc-1",
                        profile_id="p1", clone_version=1,
                        context_text="今天加班到十点，好累",
                        target_text="啊这也太晚了|||快回去歇着",
                        scene="comfort", quality=0.9,
                        started_at=datetime(2024, 3, 15, tzinfo=timezone.utc),
                        ended_at=datetime(2024, 3, 15, 0, 1, tzinfo=timezone.utc),
                    ),
                    CloneEpisodeInDB(
                        dataset_id=dataset.id, account_id="acc-1",
                        profile_id="p1", clone_version=1,
                        context_text="我升职了！！！",
                        target_text="真的假的！请客",
                        scene="celebrate", quality=0.9,
                        started_at=datetime(2024, 3, 15, tzinfo=timezone.utc),
                        ended_at=datetime(2024, 3, 15, 0, 1, tzinfo=timezone.utc),
                    ),
                ])
                profile = PromptProfileInDB(
                    _id="p1", slug="p1", name="张三", account_id="acc-1",
                    visibility="private", clone_enabled=True,
                    clone_status="active", clone_dataset_id=dataset.id,
                    clone_version=1,
                )
                ctx = await retrieve_clone_context(
                    profile, text="又加班到十一点，饭都没吃", history=[]
                )
                assert len(ctx.examples) == 1
                assert "太晚" in ctx.examples[0].target_text
                hint = render_clone_context(ctx)
                assert "不是当前发生的事实" in hint

                profile.clone_version = 2
                assert (await retrieve_clone_context(
                    profile, text="加班", history=[]
                )).empty

                profile.clone_version = 1
                profile.account_id = "acc-2"
                assert (await retrieve_clone_context(
                    profile, text="加班", history=[]
                )).empty
            finally:
                settings.clone_retrieval_enabled = old_flag
                mongo._db = None

        _run(body())


class TestCloneImportSave:
    def test_upload_opt_in_creates_expiring_pending_corpus(self) -> None:
        async def body():
            _fresh_db("test_clone_upload")
            from app.admin.auth import require_admin
            from app.chat_import import analyzer
            from app.db.repositories.clone_repo import (
                CloneDatasetRepository,
                CloneEpisodeRepository,
            )
            from app.main import app

            app.dependency_overrides[require_admin] = lambda: "admin"
            analyzer._tasks.clear()
            data = json.dumps([
                {"timestamp": "2024-03-15T14:00:00", "sender": "我",
                 "content": "在吗", "is_target": False},
                {"timestamp": "2024-03-15T14:01:00", "sender": "张三",
                 "content": "在呀", "is_target": True},
            ]).encode("utf-8")
            try:
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    with patch(
                        "app.admin.routes.chat_import.spawn",
                        lambda coro: coro.close(),
                    ):
                        resp = await client.post(
                            "/admin/chat-import/upload",
                            files={"file": ("chat.json", data, "application/json")},
                            data={
                                "role_name": "小张",
                                "store_clone_corpus": "on",
                            },
                        )
                assert resp.status_code == 200
                task = next(iter(analyzer._tasks.values()))
                assert task.clone_dataset_id
                dataset = await CloneDatasetRepository().find_by_id(
                    task.clone_dataset_id
                )
                assert dataset is not None and dataset.status == "pending"
                assert dataset.expires_at is not None
                episodes = await CloneEpisodeRepository().list_by_dataset(dataset.id)
                assert len(episodes) == 1
                assert episodes[0].expires_at is not None
                assert episodes[0].profile_id == ""
            finally:
                analyzer._tasks.clear()
                app.dependency_overrides.clear()
                mongo._db = None

        _run(body())

    def test_save_activates_pending_dataset_and_binds_episode(self) -> None:
        async def body():
            _fresh_db("test_clone_save")
            from app.admin.auth import require_admin
            from app.chat_import import analyzer
            from app.db.repositories.clone_repo import (
                CloneDatasetRepository,
                CloneEpisodeRepository,
            )
            from app.main import app

            app.dependency_overrides[require_admin] = lambda: "admin"
            expiry = datetime(2024, 3, 16, tzinfo=timezone.utc)
            dataset = CloneDatasetInDB(
                task_id="t-clone", opt_in=True, expires_at=expiry
            )
            await CloneDatasetRepository().create(dataset)
            await CloneEpisodeRepository().create_many([
                CloneEpisodeInDB(
                    dataset_id=dataset.id,
                    context_text="在吗",
                    target_text="在呀",
                    scene="greeting",
                    started_at=datetime(2024, 3, 15, tzinfo=timezone.utc),
                    ended_at=datetime(2024, 3, 15, 0, 1, tzinfo=timezone.utc),
                    expires_at=expiry,
                )
            ])
            analyzer._tasks.clear()
            analyzer._tasks["t-clone"] = AnalysisTask(
                task_id="t-clone",
                status="done",
                clone_dataset_id=dataset.id,
                clone_episode_count=1,
                result={
                    "slug": "clone-role", "name": "克隆角色",
                    "source": "聊天记录分析", "category": "custom",
                    "visibility": "private", "account_id": "acc-1",
                    "is_active": True,
                    "persona": {
                        "background": "", "era": "现代",
                        "age_appearance": "", "appearance": "",
                        "core_values": [], "relationships": [],
                    },
                    "language_style": {}, "personality": {},
                    "dialogue_strategy": {}, "meta_instructions": "",
                },
            )
            try:
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    resp = await client.post("/admin/chat-import/save", data={
                        "slug": "clone-role",
                        "name": "克隆角色",
                        "task_id": "t-clone",
                        "visibility": "private",
                        "account_id": "acc-1",
                        "is_active": "on",
                        "persona_relationships": "[]",
                        "trait_key": "warmth",
                        "trait_value": "0.8",
                    })
                assert resp.status_code == 303

                saved = await CloneDatasetRepository().find_by_id(dataset.id)
                assert saved is not None
                assert saved.status == "active"
                assert saved.version == 1
                assert saved.expires_at is None
                episodes = await CloneEpisodeRepository().list_by_dataset(dataset.id)
                assert episodes[0].profile_id
                assert episodes[0].clone_version == 1
                assert episodes[0].account_id == "acc-1"
                assert episodes[0].expires_at is None

                from app.db.repositories.profile_repo import ProfileRepository

                profile = await ProfileRepository().find_by_slug("clone-role")
                assert profile is not None
                assert profile.clone_enabled is True
                assert profile.clone_status == "active"
                assert profile.clone_version == 1

                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    resp = await client.post(
                        "/admin/profiles/clone-role/clone-delete",
                        data={"confirm": "delete"},
                    )
                assert resp.status_code == 200
                assert "克隆语料" in resp.text
                cleared = await ProfileRepository().find_by_slug("clone-role")
                assert cleared is not None
                assert cleared.clone_status == ""
                assert cleared.clone_dataset_id == ""
                assert await CloneDatasetRepository().find_by_id(dataset.id) is None
                assert await CloneEpisodeRepository().list_by_dataset(dataset.id) == []
            finally:
                analyzer._tasks.clear()
                app.dependency_overrides.clear()
                mongo._db = None

        _run(body())

    def test_public_profile_cannot_activate_corpus(self) -> None:
        async def body():
            _fresh_db("test_clone_public")
            from app.admin.auth import require_admin
            from app.chat_import import analyzer
            from app.db.repositories.clone_repo import CloneDatasetRepository
            from app.main import app

            app.dependency_overrides[require_admin] = lambda: "admin"
            dataset = CloneDatasetInDB(task_id="t-pub", opt_in=True)
            await CloneDatasetRepository().create(dataset)
            analyzer._tasks.clear()
            analyzer._tasks["t-pub"] = AnalysisTask(
                task_id="t-pub", status="done", clone_dataset_id=dataset.id,
                result={"slug": "pub-role", "name": "公开角色", "persona": {}},
            )
            try:
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    resp = await client.post("/admin/chat-import/save", data={
                        "slug": "pub-role", "name": "公开角色",
                        "task_id": "t-pub", "visibility": "public",
                    })
                assert resp.status_code == 200
                assert "必须选择“私有”" in resp.text
            finally:
                analyzer._tasks.clear()
                app.dependency_overrides.clear()
                mongo._db = None

        _run(body())


class TestPromptAndCascade:
    def test_clone_hint_is_dynamic_tail_and_examples_stable(self) -> None:
        from app.llm.prompt_assembler import PromptAssembler
        from app.models.profile import (
            ProfileLanguageStyle,
            PromptProfileInDB,
        )

        profile = PromptProfileInDB(
            _id="p1", slug="p1", name="张三",
            language_style=ProfileLanguageStyle(
                example_utterances=["第一条", "第二条", "第三条"]
            ),
            clone_style_hint="真实回复中位约 5 字。",
        )
        messages = PromptAssembler().assemble(
            profile=profile,
            history=[],
            identity_input="第三条",
            clone_hint="【相似情境下的表达参考】\n示例正文",
        )
        assert "第一条" in messages[0]["content"]
        assert "第二条" in messages[0]["content"]
        assert "第三条" not in messages[0]["content"]
        assert any("示例正文" in m["content"] for m in messages)

    def test_hard_delete_cascades_clone_data(self) -> None:
        async def body():
            _fresh_db("test_clone_cascade")
            from app.db.repositories.clone_repo import (
                CloneDatasetRepository,
                CloneEpisodeRepository,
                CloneFeedbackRepository,
            )
            from app.db.repositories.profile_repo import ProfileRepository
            from app.models.clone import CloneFeedbackInDB
            from app.models.profile import PromptProfileCreate

            try:
                profile = await ProfileRepository().create(PromptProfileCreate(
                    slug="gone", name="Gone", visibility="private",
                    account_id="acc",
                ))
                dataset = CloneDatasetInDB(
                    account_id="acc", profile_id=profile.id, version=1,
                    status="active",
                )
                await CloneDatasetRepository().create(dataset)
                await CloneEpisodeRepository().create_many([
                    CloneEpisodeInDB(
                        dataset_id=dataset.id, account_id="acc",
                        profile_id=profile.id, clone_version=1,
                        context_text="a", target_text="b",
                        started_at=datetime(2024, 3, 15, tzinfo=timezone.utc),
                        ended_at=datetime(2024, 3, 15, 0, 1, tzinfo=timezone.utc),
                    )
                ])
                await CloneFeedbackRepository().create(CloneFeedbackInDB(
                    account_id="acc", service_id="s1",
                    profile_id=profile.id, rating="like",
                ))
                assert await ProfileRepository().hard_delete("gone") is True
                assert await CloneDatasetRepository().find_by_id(dataset.id) is None
                assert await CloneEpisodeRepository().list_by_dataset(dataset.id) == []
                db = mongo.get_database()
                assert await db.clone_feedback.count_documents({}) == 0
            finally:
                mongo._db = None

        _run(body())
