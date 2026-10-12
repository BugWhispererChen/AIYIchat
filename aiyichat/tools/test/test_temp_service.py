"""临时服务管理模块测试。"""

import asyncio
from unittest.mock import patch

import mongomock_motor

from app.db import mongo
from app.services.temp_service import (
    TEMP_SERVICE_PREFIX,
    cleanup_temp_service,
    create_temp_service,
    verify_bot_binding,
)


def _run(coro) -> None:
    asyncio.run(coro)


class TestTempService:
    """临时服务管理测试。"""

    async def _setup_db(self):
        """设置测试数据库。"""
        mongo._db = mongomock_motor.AsyncMongoMockClient()["test_temp_service"]

    async def _teardown_db(self):
        """清理测试数据库。"""
        mongo._db = None

    def test_create_temp_service_success(self) -> None:
        """测试成功创建临时服务。"""

        async def body():
            await self._setup_db()
            try:
                # 创建测试数据
                from app.db.repositories.profile_repo import ProfileRepository
                from app.models.profile import PromptProfileCreate

                # 创建默认角色
                profile_repo = ProfileRepository()
                profile = await profile_repo.create(
                    PromptProfileCreate(
                        slug="test-profile",
                        name="测试角色",
                        category="original",
                    )
                )

                # 创建临时服务
                service = await create_temp_service("bot-1", "acc-1")

                assert service is not None
                assert service.name.startswith(TEMP_SERVICE_PREFIX)
                assert service.bot_id == "bot-1"
                assert service.account_id == "acc-1"
                # 临时服务会创建新的默认用户，所以user_id不为空即可
                assert service.user_id != ""
                assert service.profile_id == profile.slug
                assert service.is_active is True

            finally:
                await self._teardown_db()

        _run(body())

    def test_create_temp_service_no_profile(self) -> None:
        """测试无可用角色时创建临时服务失败。"""

        async def body():
            await self._setup_db()
            try:
                # 不创建角色，直接创建临时服务
                service = await create_temp_service("bot-1", "acc-1")
                assert service is None

            finally:
                await self._teardown_db()

        _run(body())

    def test_cleanup_temp_service_success(self) -> None:
        """测试成功清理临时服务。"""

        async def body():
            await self._setup_db()
            try:
                from app.db.repositories.profile_repo import ProfileRepository
                from app.db.repositories.service_repo import ServiceRepository
                from app.db.repositories.user_repo import UserRepository
                from app.models.profile import PromptProfileCreate
                from app.models.service import ServiceCreate
                from app.models.user import UserCreate

                # 创建测试数据
                user_repo = UserRepository()
                user = await user_repo.create(
                    UserCreate(
                        account_id="acc-1",
                        name="测试用户",
                        platform_id="test_qq_user",
                        platform="qq",
                    )
                )

                profile_repo = ProfileRepository()
                profile = await profile_repo.create(
                    PromptProfileCreate(
                        slug="test-profile",
                        name="测试角色",
                        category="original",
                    )
                )

                service_repo = ServiceRepository()
                service = await service_repo.create(
                    ServiceCreate(
                        name=f"{TEMP_SERVICE_PREFIX}测试",
                        account_id="acc-1",
                        user_id=user.id,
                        bot_id="bot-1",
                        profile_id=profile.slug,
                    )
                )

                # 清理临时服务
                result = await cleanup_temp_service(service.id)
                assert result is True

                # 验证服务已删除
                deleted_service = await service_repo.find_by_id(service.id)
                assert deleted_service is None

            finally:
                await self._teardown_db()

        _run(body())

    def test_verify_bot_binding_success(self) -> None:
        """测试Bot绑定验证成功流程。"""

        async def body():
            await self._setup_db()
            try:
                # 创建测试数据
                from app.db.repositories.profile_repo import ProfileRepository
                from app.db.repositories.user_repo import UserRepository
                from app.models.profile import PromptProfileCreate
                from app.models.user import UserCreate

                user_repo = UserRepository()
                await user_repo.create(
                    UserCreate(
                        account_id="acc-1",
                        name="测试用户",
                        platform_id="test_qq_user",
                        platform="qq",
                    )
                )

                profile_repo = ProfileRepository()
                await profile_repo.create(
                    PromptProfileCreate(
                        slug="test-profile",
                        name="测试角色",
                        category="original",
                    )
                )

                # Mock BotManager和QQ客户端
                with patch(
                    "app.services.temp_service.wait_for_bot_ready"
                ) as mock_wait:
                    mock_wait.return_value = True

                    with patch(
                        "app.services.temp_service.send_test_message"
                    ) as mock_send:
                        mock_send.return_value = True

                        result = await verify_bot_binding("bot-1", "acc-1")

                        assert result["status"] == "success"
                        assert "验证通过" in result["message"]

            finally:
                await self._teardown_db()

        _run(body())

    def test_verify_bot_binding_no_profile(self) -> None:
        """测试无角色时Bot绑定验证部分成功。"""

        async def body():
            await self._setup_db()
            try:
                # 不创建角色
                result = await verify_bot_binding("bot-1", "acc-1")

                assert result["status"] == "partial"
                assert "无法创建验证服务" in result["message"]

            finally:
                await self._teardown_db()

        _run(body())

    def test_verify_bot_binding_bot_not_ready(self) -> None:
        """测试Bot未就绪时绑定验证部分成功。"""

        async def body():
            await self._setup_db()
            try:
                # 创建测试数据
                from app.db.repositories.profile_repo import ProfileRepository
                from app.db.repositories.user_repo import UserRepository
                from app.models.profile import PromptProfileCreate
                from app.models.user import UserCreate

                user_repo = UserRepository()
                await user_repo.create(
                    UserCreate(
                        account_id="acc-1",
                        name="测试用户",
                        platform_id="test_qq_user",
                        platform="qq",
                    )
                )

                profile_repo = ProfileRepository()
                await profile_repo.create(
                    PromptProfileCreate(
                        slug="test-profile",
                        name="测试角色",
                        category="original",
                    )
                )

                # Mock Bot未就绪
                with patch(
                    "app.services.temp_service.wait_for_bot_ready"
                ) as mock_wait:
                    mock_wait.return_value = False

                    result = await verify_bot_binding("bot-1", "acc-1")

                    assert result["status"] == "partial"
                    assert "启动中" in result["message"]

            finally:
                await self._teardown_db()

        _run(body())


class TestQQBindRoutesWithVerification:
    """QQ扫码绑定路由验证测试。"""

    async def _setup_app(self):
        """设置测试应用。"""
        mongo._db = mongomock_motor.AsyncMongoMockClient()["test_qq_bind_verify"]
        from app.admin.auth import require_admin
        from app.main import app

        app.dependency_overrides[require_admin] = lambda: "admin"
        return app

    def _teardown_app(self, app) -> None:
        """清理测试应用。"""
        app.dependency_overrides.clear()
        mongo._db = None

    def _client(self, app):
        """创建测试客户端。"""
        from httpx import ASGITransport, AsyncClient

        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    def test_bind_success_with_verification(self) -> None:
        """测试绑定成功后触发验证流程。"""

        async def body():
            app = await self._setup_app()
            try:
                async with self._client(app) as client:
                    from app.admin.routes.common import _bot_config_repo
                    from app.admin.routes.qq_bots import _pending_sessions
                    from app.models.bot_config import BotConfigCreate

                    # 创建Bot配置
                    bot = await _bot_config_repo.create(
                        BotConfigCreate(
                            account_id="acc-1",
                            name="测试QQBot",
                            platform="qq",
                        )
                    )

                    # 创建绑定会话
                    from app.adapters.qq_bind import QQBindSession

                    session = QQBindSession(
                        task_id="test-task",
                        bind_key="dGVzdC1rZXk=",  # base64 test-key
                        qr_url="https://example.com/qr",
                    )
                    _pending_sessions[bot.id] = session

                    # Mock平台返回绑定成功
                    from tools.test.test_adapter_qq_bind import (
                        FakeHttpxClient,
                        FakeHttpxResponse,
                    )

                    FakeHttpxClient._response = FakeHttpxResponse(
                        json_data={
                            "retcode": 0,
                            "data": {
                                "status": 2,  # STATUS_COMPLETED
                                "bot_appid": "1024",
                                "bot_encrypt_secret": "encrypted-secret",
                            },
                        }
                    )

                    # Mock解密和验证
                    with patch(
                        "app.adapters.qq_bind.httpx.AsyncClient", FakeHttpxClient
                    ):
                        with patch(
                            "app.adapters.qq_bind.decrypt_app_secret"
                        ) as mock_decrypt:
                            mock_decrypt.return_value = "decrypted-secret"

                            with patch(
                                "app.admin.routes.qq_bots.verify_bot_binding"
                            ) as mock_verify:
                                mock_verify.return_value = {
                                    "status": "success",
                                    "message": "验证通过",
                                }

                                resp = await client.get(
                                    f"/admin/qq-bots/{bot.id}/poll-qr"
                                )

                                assert resp.status_code == 200
                                data = resp.json()
                                assert data["status"] == "verifying"
                                assert "正在验证" in data["message"]

                                # 验证凭据已保存
                                updated_bot = await _bot_config_repo.find_by_id(
                                    bot.id
                                )
                                assert updated_bot.app_id == "1024"
                                assert updated_bot.app_secret == "decrypted-secret"

                    _pending_sessions.pop(bot.id, None)

            finally:
                self._teardown_app(app)

        _run(body())
