"""聊天记录导入流水线与剩余路由/模型测试。

覆盖：
- chat_import.parser：文件保存、JSON 解析、时间戳、摘要
- chat_import.analyzer：任务 CRUD、分块、JSON 解析、结构补全、slug、run_analysis
- admin/routes/chat_import：上传/状态/预览/保存/下载
- admin/routes/features / settings / wechat_bots
- models：media / conversation from_mongo
"""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import mongomock_motor
from httpx import ASGITransport, AsyncClient

from app.db import mongo


def _run(coro):
    return asyncio.run(coro)


def _fresh_db(name="test_chat_import"):
    mongo._db = mongomock_motor.AsyncMongoMockClient()[name]


# ── parser ────────────────────────────────────────────────


class TestParser:
    """JSON 解析器。"""

    def test_parse_json_file(self, tmp_path) -> None:
        from app.chat_import.parser import parse_json_file, save_uploaded_file

        data = [
            {"timestamp": "2024-03-15T14:23:01", "sender": "张三", "content": "你好呀", "is_target": True},
            {"timestamp": "2024-03-15T14:24:01", "sender": "我", "content": "在的", "is_target": False},
            {"timestamp": 1710000000, "sender": "张三", "content": "最近如何", "is_target": True},
            {"timestamp": "2024-03-15T14:25:01", "sender": "张三", "content": "[图片]", "is_target": True},
            "非字典项",
        ]
        path = save_uploaded_file(json.dumps(data).encode("utf-8"), "task-parse")
        try:
            messages, result = parse_json_file(path)
            assert result.total_count >= 2
            assert result.target_name == "张三"
            assert result.target_count >= 1
        finally:
            path.unlink(missing_ok=True)

    def test_parse_errors(self, tmp_path) -> None:
        from app.chat_import.parser import parse_json_file, save_uploaded_file

        # 非数组
        p1 = save_uploaded_file(b'{"a":1}', "t1")
        try:
            try:
                parse_json_file(p1)
                raise AssertionError("应抛 ValueError")
            except ValueError:
                pass
        finally:
            p1.unlink(missing_ok=True)

        # 空数组
        p2 = save_uploaded_file(b"[]", "t2")
        try:
            try:
                parse_json_file(p2)
                raise AssertionError("应抛 ValueError")
            except ValueError:
                pass
        finally:
            p2.unlink(missing_ok=True)

        # 非法 JSON
        p3 = save_uploaded_file(b"not-json", "t3")
        try:
            try:
                parse_json_file(p3)
                raise AssertionError("应抛 ValueError")
            except ValueError:
                pass
        finally:
            p3.unlink(missing_ok=True)

    def test_parse_timestamp(self) -> None:
        """时间戳按导入时区（默认 UTC+8）转 aware UTC，修正存量 8 小时错位。"""
        from datetime import timezone

        from app.chat_import.parser import _parse_timestamp

        # 北京 2024-03-15 14:23:01 → UTC 06:23:01
        ts = _parse_timestamp("2024-03-15T14:23:01")
        assert ts.year == 2024
        assert ts.tzinfo is not None  # 必须为 aware，不再是 naive
        assert ts.utcoffset() == timezone.utc.utcoffset(None)
        assert ts.hour == 6  # 14:23 北京 → 06:23 UTC
        assert _parse_timestamp("2024-03-15 14:23").hour == 6
        try:
            _parse_timestamp("bad-ts")
            raise AssertionError("应抛 ValueError")
        except ValueError:
            pass

    def test_parse_message_filters(self) -> None:
        from app.chat_import.parser import _parse_message

        # 单字回复是风格证据，不再丢弃
        short = _parse_message({"content": "a", "sender": "x", "timestamp": "2024-03-15T14:23:01"})
        assert short is not None and short.content == "a"
        # 媒体占位保留为 media，供风格统计使用
        media = _parse_message({"content": "[红包]", "sender": "x", "timestamp": "2024-03-15T14:23:01"})
        assert media is not None and media.message_type == "media"
        # 系统事件不属于任何一方
        assert _parse_message({"content": "[系统消息]", "sender": "x", "timestamp": "2024-03-15T14:23:01"}) is None
        # 无 sender
        assert _parse_message({"content": "你好呀", "sender": "", "timestamp": "2024-03-15T14:23:01"}) is None
        # 非法时间戳类型
        assert _parse_message({"content": "你好呀", "sender": "x", "timestamp": []}) is None
        # 字符串 "false" 不能被当成真值
        target = _parse_message({
            "content": "你好呀", "sender": "x",
            "timestamp": "2024-03-15T14:23:01", "is_target": "false",
        })
        assert target is not None and target.is_target is False


# ── analyzer ──────────────────────────────────────────────


class TestAnalyzer:
    """LLM 分析器。"""

    def test_parse_json_response(self) -> None:
        from app.chat_import.analyzer import _parse_json_response

        assert _parse_json_response('{"a": 1}') == {"a": 1}
        assert _parse_json_response('```json\n{"a": 1}\n```') == {"a": 1}
        assert _parse_json_response('前面有文字 {"a": 1} 后面') == {"a": 1}
        assert _parse_json_response("no json here") is None

    def test_ensure_profile_structure(self) -> None:
        from app.chat_import.analyzer import _ensure_profile_structure

        out = _ensure_profile_structure({"personality": {"traits": {"开朗": "0.8"}}})
        assert out["persona"]["era"] == "现代"
        assert out["personality"]["traits"]["开朗"] == 0.8
        assert "safety_guardrails" in out["dialogue_strategy"]

    def test_generate_slug(self) -> None:
        from app.chat_import.analyzer import _generate_slug

        assert _generate_slug("Alice").startswith("alice-")
        assert _generate_slug("张三").startswith("chat-role-")

    def test_split_into_chunks(self) -> None:
        from app.chat_import.analyzer import _split_into_chunks
        from app.chat_import.models import ChatMessage
        from app.config import settings

        old = settings.chat_import_chunk_size
        settings.chat_import_chunk_size = 2
        try:
            msgs = [
                ChatMessage(
                    timestamp=datetime(2024, 3, 15, 14, i), sender="张三",
                    content=f"消息{i}", is_target=True,
                )
                for i in range(5)
            ]
            chunks = _split_into_chunks(msgs, "张三")
            assert len(chunks) >= 2
        finally:
            settings.chat_import_chunk_size = old

    def test_task_crud(self) -> None:
        async def body():
            _fresh_db()
            try:
                from app.chat_import.analyzer import (
                    _tasks,
                    create_task,
                    get_task,
                    save_task,
                )

                _tasks.clear()
                task = await create_task("t-1", "/path/file.json")
                assert task.task_id == "t-1"
                # 内存命中
                assert (await get_task("t-1")).task_id == "t-1"
                # 清空内存后从库恢复
                _tasks.clear()
                restored = await get_task("t-1")
                assert restored is not None and restored.task_id == "t-1"
                # 不存在
                assert await get_task("no-such") is None
                await save_task(task)
            finally:
                mongo._db = None

        _run(body())

    def test_run_analysis(self) -> None:
        async def body():
            _fresh_db()
            try:
                from app.chat_import import analyzer
                from app.chat_import.analyzer import create_task, get_task, run_analysis
                from app.chat_import.models import ChatMessage

                analyzer._tasks.clear()
                await create_task("t-run", "/path")

                msgs = [
                    ChatMessage(
                        timestamp=datetime(2024, 3, 15, 14, i), sender="张三",
                        content=f"我今天很开心消息{i}", is_target=True,
                    )
                    for i in range(3)
                ]
                chunk_json = json.dumps({"tone": "温柔", "traits": {}})
                merge_json = json.dumps({
                    "persona": {"background": "背景"},
                    "personality": {"traits": {"开朗": 0.8}},
                })
                fake_provider = SimpleNamespace(complete=AsyncMock(
                    side_effect=[chunk_json, merge_json]
                ))
                with patch(
                    "app.chat_import.analyzer.get_llm_provider",
                    return_value=fake_provider,
                ):
                    await run_analysis("t-run", msgs, role_name="小张")
                task = await get_task("t-run")
                assert task is not None and task.status == "done"
                assert task.result["name"] == "小张"
            finally:
                mongo._db = None

        _run(body())

    def test_run_analysis_no_target(self) -> None:
        async def body():
            _fresh_db()
            try:
                from app.chat_import import analyzer
                from app.chat_import.analyzer import create_task, get_task, run_analysis
                from app.chat_import.models import ChatMessage

                analyzer._tasks.clear()
                await create_task("t-err", "/path")
                # 无 is_target 消息 → 报错
                msgs = [
                    ChatMessage(
                        timestamp=datetime(2024, 3, 15, 14, 0), sender="我",
                        content="随便说说", is_target=False,
                    )
                ]
                await run_analysis("t-err", msgs)
                task = await get_task("t-err")
                assert task is not None and task.status == "error"
            finally:
                mongo._db = None

        _run(body())


# ── admin routes（chat_import / features / settings / wechat_bots）────


async def _setup_app(override_auth=True):
    _fresh_db("test_ci_routes")
    from app.admin.auth import require_admin
    from app.main import app

    if override_auth:
        app.dependency_overrides[require_admin] = lambda: "admin"
    return app


async def _teardown(app):
    app.dependency_overrides.clear()
    mongo._db = None


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class TestChatImportRoutes:
    """聊天导入路由。"""

    def test_page_and_upload_validation(self) -> None:
        async def body():
            app = await _setup_app()
            try:
                async with _client(app) as client:
                    resp = await client.get("/admin/chat-import")
                    assert resp.status_code == 200

                    # 无文件
                    resp = await client.post("/admin/chat-import/upload", data={})
                    assert resp.status_code == 200 and "请选择" in resp.text

                    # 非 JSON 文件
                    resp = await client.post(
                        "/admin/chat-import/upload",
                        files={"file": ("a.txt", b"x", "text/plain")},
                    )
                    assert resp.status_code == 200 and "仅支持JSON" in resp.text
            finally:
                await _teardown(app)

        _run(body())

    def test_upload_and_status_preview(self) -> None:
        async def body():
            app = await _setup_app()
            try:
                data = json.dumps([
                    {"timestamp": "2024-03-15T14:23:01", "sender": "张三",
                     "content": "你好呀最近怎么样", "is_target": True},
                    {"timestamp": "2024-03-15T14:24:01", "sender": "张三",
                     "content": "我今天很开心呢", "is_target": True},
                ]).encode("utf-8")
                async with _client(app) as client:
                    # spawn 后台分析：拦截为 no-op，避免真实 LLM
                    with patch(
                        "app.admin.routes.chat_import.spawn",
                        lambda coro: coro.close(),
                    ):
                        resp = await client.post(
                            "/admin/chat-import/upload",
                            files={"file": ("chat.json", data, "application/json")},
                            data={"role_name": "小张"},
                        )
                    assert resp.status_code == 200 and "task_id" in resp.text or resp.status_code == 200

                    # 状态查询：不存在任务
                    resp = await client.get("/admin/chat-import/status/no-such")
                    assert resp.status_code == 200

                    # 预览：任务不存在 → 404
                    resp = await client.get("/admin/chat-import/preview/no-such")
                    assert resp.status_code == 404
            finally:
                await _teardown(app)

        _run(body())

    def test_save_validation(self) -> None:
        async def body():
            app = await _setup_app()
            try:
                async with _client(app) as client:
                    # 缺少 slug → 400
                    resp = await client.post("/admin/chat-import/save", data={"name": "x"})
                    assert resp.status_code == 400

                    # 正常保存 → 重定向到详情
                    resp = await client.post("/admin/chat-import/save", data={
                        "slug": "new-role", "name": "新角色",
                        "trait_key": "开朗", "trait_value": "0.8",
                    })
                    assert resp.status_code == 303
            finally:
                await _teardown(app)

        _run(body())


class TestFeaturesAndSettings:
    """能力开关与热重载路由。"""

    def test_features_page_and_save(self) -> None:
        async def body():
            from app.admin.routes.features import FEATURE_TOGGLES
            from app.config import settings

            app = await _setup_app()
            # 保存能力开关的路由会把 FEATURE_TOGGLES 里的每个键就地写进 settings
            # （表单里没勾的即为关闭）。不复原就会污染同进程后续测试——
            # 新登记的开关默认开着，跑到这里会被静默翻成 False。
            snapshot = {f.key: getattr(settings, f.key) for f in FEATURE_TOGGLES}
            try:
                async with _client(app) as client:
                    resp = await client.get("/admin/features")
                    assert resp.status_code == 200

                    resp = await client.post("/admin/features", data={
                        "support_strategy_enabled": "on",
                        "embedding_enabled": "on",
                    })
                    assert resp.status_code == 200 and "已保存并生效" in resp.text
            finally:
                for key, value in snapshot.items():
                    setattr(settings, key, value)
                await _teardown(app)

        _run(body())

    def test_settings_reload(self) -> None:
        async def body():
            app = await _setup_app()
            try:
                async with _client(app) as client:
                    resp = await client.post("/admin/api/reload-settings")
                    assert resp.status_code == 200
                    assert resp.json()["status"] == "ok"
            finally:
                await _teardown(app)

        _run(body())


class TestWechatBotRoutes:
    """微信 Bot 扫码路由。"""

    def test_qrcode_poll_complete(self) -> None:
        async def body():
            app = await _setup_app()
            try:
                from app.adapters import wechat_clawbot
                from app.db.repositories.bot_config_repo import BotConfigRepository
                from app.models.bot_config import BotConfigCreate

                bot = await BotConfigRepository().create(
                    BotConfigCreate(account_id="acc-1", name="微信Bot", platform="wechat")
                )

                # 用一个 fake client 注入 manager
                fake_client = SimpleNamespace(
                    get_login_qrcode=AsyncMock(return_value={
                        "qrcode": "QR", "qrcode_url": "data:image/png;base64,x",
                    }),
                    poll_qrcode_status=AsyncMock(return_value={
                        "status": "confirmed", "bot_token": "BT",
                    }),
                    set_bot_token=lambda t: None,
                    complete_login=AsyncMock(return_value={}),
                )
                fake_manager = SimpleNamespace(
                    get_client=lambda cid: fake_client,
                    is_bot_running=lambda cid: False,
                    start_bot_task=lambda cid, c: None,
                    cancel_bot_task=AsyncMock(),
                    _on_token_update=AsyncMock(),
                    _pending_clients={},
                )
                with patch.object(
                    wechat_clawbot, "get_wechat_manager", lambda: fake_manager
                ):
                    async with _client(app) as client:
                        # 获取二维码
                        resp = await client.post(
                            f"/admin/wechat-bots/{bot.id}/qrcode"
                        )
                        assert resp.status_code == 200 and "二维码" in resp.text

                        # 轮询扫码：成功
                        resp = await client.get(
                            f"/admin/wechat-bots/{bot.id}/poll-qr",
                            params={"qrcode": "QR"},
                        )
                        assert resp.status_code == 200
                        assert resp.json()["status"] == "success"

                        # 完成登录
                        resp = await client.post(
                            f"/admin/wechat-bots/{bot.id}/complete-login",
                            data={"bot_token": "BT"},
                        )
                        assert resp.status_code == 200
                        assert resp.json()["status"] == "success"
            finally:
                await _teardown(app)

        _run(body())

    def test_client_not_found(self) -> None:
        async def body():
            app = await _setup_app()
            try:
                from app.adapters import wechat_clawbot

                fake_manager = SimpleNamespace(get_client=lambda cid: None)
                with patch.object(
                    wechat_clawbot, "get_wechat_manager", lambda: fake_manager
                ):
                    async with _client(app) as client:
                        # poll-qr 客户端不存在 → 404
                        resp = await client.get(
                            "/admin/wechat-bots/none/poll-qr",
                            params={"qrcode": "QR"},
                        )
                        assert resp.status_code == 404
                        # complete-login 客户端不存在 → 404
                        resp = await client.post(
                            "/admin/wechat-bots/none/complete-login",
                            data={"bot_token": "BT"},
                        )
                        assert resp.status_code == 404
            finally:
                await _teardown(app)

        _run(body())


# ── models ────────────────────────────────────────────────


class TestModels:
    """模型 from_mongo。"""

    def test_media_from_mongo(self) -> None:
        from app.models.media import MediaFileInDB

        doc = {
            "_id": "abc", "content_type": "image/png", "filename": "a.png",
            "size": 100, "data": b"xxx",
        }
        m = MediaFileInDB.from_mongo(doc)
        assert m.id == "abc" and m.content_type == "image/png"
        try:
            MediaFileInDB.from_mongo(None)
            raise AssertionError("应抛 ValueError")
        except ValueError:
            pass

    def test_conversation_from_mongo(self) -> None:
        from app.models.conversation import ConversationInDB

        doc = {
            "_id": "c1", "identity_id": "u1", "profile_id": "p1",
            "service_id": "s1", "status": "active",
        }
        c = ConversationInDB.from_mongo(doc)
        assert c.id == "c1" and c.identity_id == "u1"
