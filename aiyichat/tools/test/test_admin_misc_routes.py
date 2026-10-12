"""Admin 其余路由与公共 API 测试（mongomock + ASGI 内存客户端）。

覆盖：
- admin/router.py：登录/登出、仪表盘、AI模型配置页、热重载、系统监控与辅助函数
- admin/routes/conversations.py：对话列表/详情/删除
- admin/routes/billing.py：套餐模板管理与开通台账
- admin/routes/profiles.py：角色列表/详情/启停/编辑/定妆照
- app/api：health / profiles / conversations / media / proactive
"""

import asyncio

import mongomock_motor
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.db import mongo
from app.db.repositories.conversation_repo import ConversationRepository
from app.db.repositories.message_repo import MessageRepository
from app.db.repositories.profile_repo import ProfileRepository
from app.db.repositories.quota_template_repo import QuotaTemplateRepository
from app.db.repositories.service_repo import ServiceRepository
from app.db.repositories.user_repo import UserRepository
from app.models.profile import PromptProfileCreate
from app.models.service import ServiceCreate
from app.models.user import UserCreate


async def _setup_app(override_auth: bool = True):
    """内存数据库 + （可选）免登录的 ASGI 应用。"""
    mongo._db = mongomock_motor.AsyncMongoMockClient()["test_admin_misc"]

    from app.admin.auth import require_admin
    from app.main import app

    if override_auth:
        app.dependency_overrides[require_admin] = lambda: "admin"
    return app


async def _teardown_app(app) -> None:
    app.dependency_overrides.clear()
    mongo._db = None


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _run(coro) -> None:
    asyncio.run(coro)


class TestAuthFlow:
    """登录/登出流程与后台鉴权失败分支。"""

    def test_login_logout(self) -> None:
        async def flow() -> None:
            app = await _setup_app(override_auth=False)
            old_pw = settings.admin_password
            settings.admin_password = "test-pw"
            try:
                async with _client(app) as client:
                    resp = await client.get("/admin/login")
                    assert resp.status_code == 200

                    # 密码错误
                    resp = await client.post(
                        "/admin/login",
                        data={"username": "admin", "password": "wrong"},
                    )
                    assert resp.status_code == 200 and "用户名或密码错误" in resp.text

                    # 登录成功 → 303 + 会话 Cookie
                    resp = await client.post(
                        "/admin/login",
                        data={"username": "admin", "password": "test-pw"},
                    )
                    assert resp.status_code == 303
                    token = resp.cookies.get("admin_session")
                    assert token

                    resp = await client.get(
                        "/admin/logout", cookies={"admin_session": token}
                    )
                    assert resp.status_code == 303

                    # 未认证：浏览器导航重定向
                    resp = await client.get(
                        "/admin/accounts", headers={"accept": "text/html"}
                    )
                    assert resp.status_code == 303
                    # fetch 请求返回 JSON 401
                    resp = await client.get("/admin/accounts")
                    assert resp.status_code == 401
                    # HTMX 请求返回提示片段
                    resp = await client.get(
                        "/admin/accounts", headers={"HX-Request": "true"}
                    )
                    assert resp.status_code == 200 and "会话已过期" in resp.text
            finally:
                settings.admin_password = old_pw
                await _teardown_app(app)

        _run(flow())


class TestPasswordChange:
    """修改密码路由。"""

    def test_change_password_flow(self) -> None:
        async def flow() -> None:
            from app.admin import auth

            app = await _setup_app(override_auth=False)
            old_pw = settings.admin_password
            settings.admin_password = "old-password-1"
            try:
                async with _client(app) as client:
                    # 登录拿到会话
                    resp = await client.post(
                        "/admin/login",
                        data={"username": "admin", "password": "old-password-1"},
                    )
                    assert resp.status_code == 303
                    token = resp.cookies.get("admin_session")
                    assert token
                    cookies = {"admin_session": token}

                    # GET 页面
                    resp = await client.get("/admin/password", cookies=cookies)
                    assert resp.status_code == 200 and "修改密码" in resp.text

                    # 当前密码错误
                    resp = await client.post("/admin/password", cookies=cookies, data={
                        "current_password": "bad",
                        "new_password": "new-password-1",
                        "confirm_password": "new-password-1",
                    })
                    assert resp.status_code == 200 and "当前密码不正确" in resp.text

                    # 新密码太短
                    resp = await client.post("/admin/password", cookies=cookies, data={
                        "current_password": "old-password-1",
                        "new_password": "short",
                        "confirm_password": "short",
                    })
                    assert resp.status_code == 200 and "至少 8 位" in resp.text

                    # 两次不一致
                    resp = await client.post("/admin/password", cookies=cookies, data={
                        "current_password": "old-password-1",
                        "new_password": "new-password-1",
                        "confirm_password": "new-password-2",
                    })
                    assert resp.status_code == 200 and "不一致" in resp.text

                    # 与旧密码相同
                    resp = await client.post("/admin/password", cookies=cookies, data={
                        "current_password": "old-password-1",
                        "new_password": "old-password-1",
                        "confirm_password": "old-password-1",
                    })
                    assert resp.status_code == 200 and "不能与当前密码相同" in resp.text

                    # 成功：303 跳登录页 + 会话失效 + settings 变为哈希
                    resp = await client.post("/admin/password", cookies=cookies, data={
                        "current_password": "old-password-1",
                        "new_password": "new-password-1",
                        "confirm_password": "new-password-1",
                    })
                    assert resp.status_code == 303
                    assert "msg=password_changed" in resp.headers.get("location", "")
                    assert token not in auth._sessions
                    assert settings.admin_password.startswith("pbkdf2$")

                    # 旧会话已失效
                    resp = await client.get("/admin/password", cookies=cookies)
                    assert resp.status_code in (303, 401)

                    # 旧密码拒绝、新密码可登录
                    resp = await client.post(
                        "/admin/login",
                        data={"username": "admin", "password": "old-password-1"},
                    )
                    assert resp.status_code == 200 and "用户名或密码错误" in resp.text
                    resp = await client.post(
                        "/admin/login",
                        data={"username": "admin", "password": "new-password-1"},
                    )
                    assert resp.status_code == 303

                    # 登录页带成功提示
                    resp = await client.get(
                        "/admin/login", params={"msg": "password_changed"}
                    )
                    assert resp.status_code == 200 and "密码已修改" in resp.text
            finally:
                settings.admin_password = old_pw
                auth._sessions.clear()
                await _teardown_app(app)

        _run(flow())


class TestDashboardAndSettings:
    """仪表盘、AI模型配置与热重载。"""
    def test_dashboard_multimedia_reload_metrics(self) -> None:
        async def flow() -> None:
            app = await _setup_app()
            try:
                async with _client(app) as client:
                    resp = await client.get("/admin/")
                    assert resp.status_code == 200

                    resp = await client.get("/admin/multimedia")
                    assert resp.status_code == 200

                    # 保存配置（基础配置组 JSON 合并 + 脱敏密钥保留 + 非法数值跳过）
                    resp = await client.post("/admin/multimedia", data={
                        "model_configs_json": (
                            '[{"id":"text-default","name":"文本",'
                            '"base_url":"http://x","api_key":"sk-abcdef123456",'
                            '"default_model":"m1"},'
                            '{"id":"","name":"无ID被跳过"}]'
                        ),
                        "chat_config_id": "text-default",
                        "chat_model": "m1",
                        "chat_temperature": "0.5",
                        "chat_max_tokens": "100",
                        "chat_timeout_seconds": "bad",
                        "asr_enabled": "on",
                    })
                    assert resp.status_code == 200 and "已保存并生效" in resp.text

                    resp = await client.post("/admin/api/reload-settings")
                    assert resp.status_code == 200 and resp.json()["status"] == "ok"

                    resp = await client.get("/admin/api/metrics")
                    assert resp.status_code == 200
            finally:
                await _teardown_app(app)

        _run(flow())

    def test_helper_functions(self) -> None:
        """路由模块内的纯函数。"""
        from datetime import datetime, timezone

        from app.admin.router import (
            _group_field,
            _mask_api_key,
            _masked_model_configs,
            _to_beijing,
        )

        assert _mask_api_key("") == ""
        assert _mask_api_key("short") == "*****"
        assert _mask_api_key("sk-abcdef123456") == "sk-a****3456"
        assert _group_field({"id": "a"}, "id") == "a"
        assert _group_field({"id": None}, "id") == ""
        masked = _masked_model_configs([{"id": "g1", "api_key": "sk-abcdef123456"}])
        assert masked[0]["has_key"] is True and "****" in masked[0]["api_key_masked"]
        assert _to_beijing(None) is None
        bj = _to_beijing(datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc))
        assert bj is not None and bj.hour == 8


class TestAdminConversations:
    """对话管理路由。"""

    def test_list_detail_delete(self) -> None:
        async def flow() -> None:
            app = await _setup_app()
            try:
                conv_repo = ConversationRepository()
                msg_repo = MessageRepository()
                user = await UserRepository().create(UserCreate(account_id="acc-1"))
                conv = await conv_repo.create(user.id, "prof-x", "svc-1", title="谈心")
                msg = await msg_repo.create(conv.id, "user", "你好")

                async with _client(app) as client:
                    resp = await client.get("/admin/conversations")
                    assert resp.status_code == 200
                    resp = await client.get(
                        "/admin/conversations", params={"q": "谈心"}
                    )
                    assert resp.status_code == 200

                    # 详情（整页 + HTMX 片段），不存在 → 404
                    resp = await client.get(f"/admin/conversations/{conv.id}")
                    assert resp.status_code == 200
                    resp = await client.get(
                        f"/admin/conversations/{conv.id}",
                        headers={"HX-Request": "true"},
                    )
                    assert resp.status_code == 200
                    resp = await client.get("/admin/conversations/" + "0" * 24)
                    assert resp.status_code == 404

                    # 删除单条消息
                    resp = await client.post(
                        f"/admin/conversations/{conv.id}/messages/{msg.id}/delete"
                    )
                    assert resp.status_code == 303
                    assert await msg_repo.count_by_conversation(conv.id) == 0

                    # 删除整个对话；再次删除 → 404
                    resp = await client.post(f"/admin/conversations/{conv.id}/delete")
                    assert resp.status_code == 303
                    resp = await client.post(f"/admin/conversations/{conv.id}/delete")
                    assert resp.status_code == 404
            finally:
                await _teardown_app(app)

        _run(flow())


class TestBilling:
    """套餐计费路由。"""

    def test_templates_and_apply(self) -> None:
        async def flow() -> None:
            app = await _setup_app()
            try:
                from app.db.repositories.account_repo import AccountRepository
                from app.db.repositories.order_repo import OrderRepository
                from app.db.repositories.quota_repo import QuotaRepository
                from app.models.account import AccountCreate

                acc = await AccountRepository().create(AccountCreate(name="账A"))
                tpl_repo = QuotaTemplateRepository()

                async with _client(app) as client:
                    resp = await client.get("/admin/billing")
                    assert resp.status_code == 200

                    # 空名称不创建
                    resp = await client.post(
                        "/admin/billing/templates/create", data={"name": " "}
                    )
                    assert resp.status_code == 303
                    assert len(await tpl_repo.list_all()) == 0

                    # 创建模板（含有效期）
                    resp = await client.post(
                        "/admin/billing/templates/create",
                        data={
                            "name": "月卡", "text_limit": "100",
                            "voice_limit": "10", "image_limit": "5",
                            "validity_days": "30",
                        },
                    )
                    assert resp.status_code == 303
                    tpl = (await tpl_repo.list_all())[0]

                    # 编辑模板
                    resp = await client.post(
                        f"/admin/billing/templates/{tpl.id}/update",
                        data={"name": "月卡v2", "text_limit": "200"},
                    )
                    assert resp.status_code == 303

                    # 无效模板 ID 开通 → 直接重定向，不记台账
                    resp = await client.post(
                        f"/admin/accounts/{acc.id}/apply-template",
                        data={"template_id": ""},
                    )
                    assert resp.status_code == 303
                    assert await OrderRepository().count() == 0

                    # 正常开通：写额度 + 记台账
                    resp = await client.post(
                        f"/admin/accounts/{acc.id}/apply-template",
                        data={"template_id": tpl.id, "amount": "9.9"},
                    )
                    assert resp.status_code == 303
                    quota = await QuotaRepository().find_by_account(acc.id)
                    assert quota is not None and quota.text_quota.limit == 200
                    assert quota.text_quota.expires_at is not None
                    assert await OrderRepository().count() == 1

                    resp = await client.get("/admin/billing")
                    assert resp.status_code == 200

                    resp = await client.post(
                        f"/admin/billing/templates/{tpl.id}/delete"
                    )
                    assert resp.status_code == 303
            finally:
                await _teardown_app(app)

        _run(flow())


class TestAdminProfiles:
    """角色管理路由。"""

    def test_list_detail_toggle_edit(self) -> None:
        async def flow() -> None:
            app = await _setup_app()
            try:
                repo = ProfileRepository()
                profile = await repo.create(
                    PromptProfileCreate(slug="hero", name="主角")
                )
                async with _client(app) as client:
                    resp = await client.get("/admin/profiles")
                    assert resp.status_code == 200 and "主角" in resp.text
                    resp = await client.get("/admin/profiles", params={"q": "主角"})
                    assert resp.status_code == 200

                    resp = await client.get("/admin/profiles/hero")
                    assert resp.status_code == 200
                    resp = await client.get("/admin/profiles/none")
                    assert resp.status_code == 404

                    # 被服务引用时禁用 → 400
                    svc = await ServiceRepository().create(ServiceCreate(
                        account_id="acc-1", user_id="u-1", bot_id="b-1",
                        profile_id=profile.id,
                    ))
                    resp = await client.post(
                        "/admin/profiles/hero/toggle",
                        data={"disable_reason": "下线"},
                    )
                    assert resp.status_code == 400
                    await ServiceRepository().delete(svc.id)

                    # 禁用成功 → 再启用
                    resp = await client.post(
                        "/admin/profiles/hero/toggle",
                        data={"disable_reason": "下线"},
                    )
                    assert resp.status_code == 200
                    disabled = await repo.find_by_slug("hero")
                    assert disabled is not None and disabled.is_active is False
                    resp = await client.post("/admin/profiles/hero/toggle")
                    assert resp.status_code == 200

                    # 编辑表单保存（trait 非法值回退 0.5）
                    resp = await client.post(
                        "/admin/profiles/hero/edit",
                        data={
                            "name": "主角v2", "is_active": "on",
                            "identity_core_values": "勇敢\n善良",
                            "rel_name": "配角", "rel_relation": "朋友",
                            "rel_attitude": "信任",
                            "trait_key": "开朗", "trait_value": "bad",
                        },
                    )
                    assert resp.status_code == 200
                    edited = await repo.find_by_slug("hero")
                    assert edited is not None and edited.name == "主角v2"
                    assert edited.personality.traits["开朗"] == 0.5
            finally:
                await _teardown_app(app)

        _run(flow())

    def test_reference_image_ops(self) -> None:
        async def flow() -> None:
            app = await _setup_app()
            try:
                repo = ProfileRepository()
                await repo.create(PromptProfileCreate(slug="hero", name="主角"))
                async with _client(app) as client:
                    # 上传非图片 → 错误提示
                    resp = await client.post(
                        "/admin/profiles/hero/reference-image/upload",
                        files={"file": ("a.txt", b"hello", "text/plain")},
                    )
                    assert resp.status_code == 200 and "请上传图片文件" in resp.text

                    # 上传空文件 → 错误提示
                    resp = await client.post(
                        "/admin/profiles/hero/reference-image/upload",
                        files={"file": ("a.png", b"", "image/png")},
                    )
                    assert resp.status_code == 200 and "上传文件为空" in resp.text

                    # 上传有效图片 → 定妆照更新
                    resp = await client.post(
                        "/admin/profiles/hero/reference-image/upload",
                        files={"file": ("a.png", b"\x89PNG-data", "image/png")},
                    )
                    assert resp.status_code == 200 and "定妆照已更新" in resp.text
                    prof = await repo.find_by_slug("hero")
                    assert prof is not None
                    assert prof.reference_image_url.startswith("/api/media/")

                    # 确认候选图：非法地址被拒 / 合法地址生效
                    resp = await client.post(
                        "/admin/profiles/hero/reference-image/confirm",
                        data={"media_url": "http://evil"},
                    )
                    assert resp.status_code == 200 and "无效的候选图地址" in resp.text
                    resp = await client.post(
                        "/admin/profiles/hero/reference-image/confirm",
                        data={"media_url": "/api/media/xyz"},
                    )
                    assert resp.status_code == 200 and "定妆照已更新" in resp.text

                    # 未填写外貌时生成 → 提示先填写
                    resp = await client.post(
                        "/admin/profiles/hero/reference-image/generate"
                    )
                    assert resp.status_code == 200 and "外貌特征" in resp.text

                    # 清除定妆照
                    resp = await client.post(
                        "/admin/profiles/hero/reference-image/clear"
                    )
                    assert resp.status_code == 200 and "定妆照已清除" in resp.text
            finally:
                await _teardown_app(app)

        _run(flow())


class TestPublicApi:
    """公共 API：health / profiles / conversations / media。"""

    def test_health_and_profiles_api(self) -> None:
        async def flow() -> None:
            app = await _setup_app(override_auth=False)
            try:
                await ProfileRepository().create(
                    PromptProfileCreate(slug="hero", name="主角")
                )
                async with _client(app) as client:
                    resp = await client.get("/api/health")
                    assert resp.status_code == 200

                    # 列表 / 详情 / 创建 / 更新 / 删除
                    resp = await client.get("/api/profiles")
                    assert resp.status_code == 200 and len(resp.json()) == 1
                    resp = await client.get("/api/profiles", params={"active_only": "false"})
                    assert resp.status_code == 200
                    resp = await client.get("/api/profiles/hero")
                    assert resp.status_code == 200 and resp.json()["slug"] == "hero"
                    resp = await client.get("/api/profiles/none")
                    assert resp.status_code == 404

                    resp = await client.post(
                        "/api/profiles",
                        json={"slug": "hero", "name": "重复"},
                    )
                    assert resp.status_code == 409
                    resp = await client.post(
                        "/api/profiles",
                        json={"slug": "villain", "name": "反派"},
                    )
                    assert resp.status_code == 201

                    resp = await client.put(
                        "/api/profiles/villain", json={"name": "反派v2"}
                    )
                    assert resp.status_code == 200 and resp.json()["updated"] is True
                    resp = await client.put(
                        "/api/profiles/none", json={"name": "x"}
                    )
                    assert resp.status_code == 404

                    resp = await client.delete("/api/profiles/villain")
                    assert resp.status_code == 200 and resp.json()["deleted"] is True
                    resp = await client.delete("/api/profiles/none")
                    assert resp.status_code == 404
            finally:
                await _teardown_app(app)

        _run(flow())

    def test_conversations_and_media_api(self) -> None:
        async def flow() -> None:
            app = await _setup_app(override_auth=False)
            try:
                conv_repo = ConversationRepository()
                msg_repo = MessageRepository()
                svc_repo = ServiceRepository()
                svc = await svc_repo.create(ServiceCreate(
                    account_id="acc-1", user_id="u-1", bot_id="b-1",
                    profile_id="p-1", share_group_id="grp-1",
                ))
                conv = await conv_repo.create("u-1", "p-1", svc.id)
                await msg_repo.create(conv.id, "user", "hi", emotion_tag="joy")

                async with _client(app) as client:
                    # 无过滤 → 空
                    resp = await client.get("/api/conversations")
                    assert resp.status_code == 200 and resp.json() == []
                    # 按用户
                    resp = await client.get(
                        "/api/conversations", params={"user_id": "u-1"}
                    )
                    assert resp.status_code == 200 and len(resp.json()) == 1
                    # 按服务（含共享组补充）
                    resp = await client.get(
                        "/api/conversations", params={"service_id": svc.id}
                    )
                    assert resp.status_code == 200

                    # 消息历史；不存在对话 → 404
                    resp = await client.get(f"/api/conversations/{conv.id}/messages")
                    assert resp.status_code == 200 and len(resp.json()) == 1
                    resp = await client.get(
                        "/api/conversations/" + "0" * 24 + "/messages"
                    )
                    assert resp.status_code == 404

                    # 媒体：不存在 → 404；存在 → 返回二进制
                    resp = await client.get("/api/media/" + "0" * 24)
                    assert resp.status_code == 404
                    from app.db.repositories.media_repo import MediaRepository
                    mid = await MediaRepository().create(b"img-bytes", "image/png")
                    resp = await client.get(f"/api/media/{mid}")
                    assert resp.status_code == 200
                    assert resp.content == b"img-bytes"
            finally:
                await _teardown_app(app)

        _run(flow())


class TestProactiveApi:
    """主动消息 API。"""

    def test_trigger_and_records(self) -> None:
        async def flow() -> None:
            app = await _setup_app(override_auth=False)
            try:
                from unittest.mock import AsyncMock

                from app.db.repositories.proactive_repo import (
                    ProactiveRecordRepository,
                )
                from app.models.proactive_record import ProactiveRecord
                from app.proactive.scheduler import get_proactive_scheduler

                user_repo = UserRepository()
                user = await user_repo.create(UserCreate(
                    account_id="acc-1", platform="qq", platform_id="oid-1",
                ))
                await ServiceRepository().create(ServiceCreate(
                    account_id="acc-1", user_id=user.id, bot_id="b-1",
                    profile_id="p-1",
                ))

                scheduler = get_proactive_scheduler()
                scheduler._send_message = AsyncMock(return_value=True)

                async with _client(app) as client:
                    # 用户不存在 → 404
                    resp = await client.post("/api/proactive/trigger", json={
                        "user_id": "no-user", "bot_id": "b-1",
                    })
                    assert resp.status_code == 404

                    # 自定义内容 + 发送成功
                    resp = await client.post("/api/proactive/trigger", json={
                        "user_id": user.id, "bot_id": "b-1",
                        "content": "想你了", "stage": 1,
                    })
                    assert resp.status_code == 200
                    body = resp.json()
                    assert body["success"] is True and body["record_id"]

                    # 发送失败分支
                    scheduler._send_message = AsyncMock(return_value=False)
                    resp = await client.post("/api/proactive/trigger", json={
                        "user_id": user.id, "bot_id": "b-1", "content": "在吗",
                    })
                    assert resp.status_code == 200 and resp.json()["success"] is False

                    # 记录查询
                    await ProactiveRecordRepository().create(ProactiveRecord(
                        user_id=user.id, platform_user_id="oid-1", bot_id="b-1",
                        stage=1,
                    ))
                    resp = await client.get("/api/proactive/records", params={
                        "user_id": user.id, "bot_id": "b-1",
                    })
                    assert resp.status_code == 200 and len(resp.json()) >= 1
            finally:
                await _teardown_app(app)

        _run(flow())
