"""QQ 扫码绑定协议测试：qq_bind / admin qq_bots 路由。

使用 monkeypatch 替换 httpx.AsyncClient，避免真实网络。
"""

import asyncio
import base64
from unittest.mock import patch

import mongomock_motor
from Crypto.Cipher import AES
from httpx import ASGITransport, AsyncClient

from app.adapters.qq_bind import (
    QQBindSession,
    create_bind_task,
    decrypt_app_secret,
    poll_bind_result,
)
from app.db import mongo


def _run(coro) -> None:
    asyncio.run(coro)


class FakeHttpxResponse:
    """伪造 httpx 响应。"""

    def __init__(self, *, json_data=None, status_code=200) -> None:
        self._json = json_data if json_data is not None else {}
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError("HTTP %d" % self.status_code)

    def json(self):
        return self._json


class FakeHttpxClient:
    """伪造 httpx.AsyncClient 异步上下文管理器。"""

    _response = FakeHttpxResponse()

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def post(self, *args, **kwargs):
        return FakeHttpxClient._response


def _make_secret_payload(bind_key_b64: str, plaintext: str) -> str:
    """构造 AES-256-GCM 密文（nonce 12B + 密文 + tag 16B）。"""
    key = base64.b64decode(bind_key_b64)
    nonce = b"\x01" * 12
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext.encode("utf-8"))
    return base64.b64encode(nonce + ciphertext + tag).decode("ascii")


BIND_KEY = base64.b64encode(b"\x07" * 32).decode("ascii")


class TestDecryptAppSecret:
    """AES-GCM 解密。"""

    def test_decrypt_ok(self) -> None:
        encrypted = _make_secret_payload(BIND_KEY, "secret-123")
        assert decrypt_app_secret(encrypted, BIND_KEY) == "secret-123"

    def test_decrypt_bad_base64(self) -> None:
        try:
            decrypt_app_secret("!!!", BIND_KEY)
            raise AssertionError("应抛 ValueError")
        except ValueError:
            pass

    def test_decrypt_short_payload(self) -> None:
        short = base64.b64encode(b"\x00" * 10).decode()
        try:
            decrypt_app_secret(short, BIND_KEY)
            raise AssertionError("应抛 ValueError")
        except ValueError:
            pass

    def test_decrypt_wrong_key(self) -> None:
        encrypted = _make_secret_payload(BIND_KEY, "secret-123")
        other_key = base64.b64encode(b"\x08" * 32).decode("ascii")
        try:
            decrypt_app_secret(encrypted, other_key)
            raise AssertionError("应抛 ValueError")
        except ValueError:
            pass


class TestCreateBindTask:
    """创建绑定任务。"""

    def test_create_ok(self) -> None:
        async def body():
            FakeHttpxClient._response = FakeHttpxResponse(json_data={
                "retcode": 0,
                "data": {"task_id": "t-1"},
            })
            with patch("app.adapters.qq_bind.httpx.AsyncClient", FakeHttpxClient):
                session = await create_bind_task()
                assert session.task_id == "t-1"
                assert "task_id=t-1" in session.qr_url
                # bind_key 为 base64 AES-256 密钥
                assert len(base64.b64decode(session.bind_key)) == 32

        _run(body())

    def test_create_retcode_fail(self) -> None:
        async def body():
            FakeHttpxClient._response = FakeHttpxResponse(json_data={
                "retcode": 100, "msg": "服务繁忙",
            })
            with patch("app.adapters.qq_bind.httpx.AsyncClient", FakeHttpxClient):
                try:
                    await create_bind_task()
                    raise AssertionError("应抛 RuntimeError")
                except RuntimeError as e:
                    assert "服务繁忙" in str(e)

        _run(body())

    def test_create_missing_task_id(self) -> None:
        async def body():
            FakeHttpxClient._response = FakeHttpxResponse(json_data={
                "retcode": 0, "data": {},
            })
            with patch("app.adapters.qq_bind.httpx.AsyncClient", FakeHttpxClient):
                try:
                    await create_bind_task()
                    raise AssertionError("应抛 RuntimeError")
                except RuntimeError:
                    pass

        _run(body())


class TestPollBindResult:
    """轮询绑定结果状态映射。"""

    def _session(self) -> QQBindSession:
        return QQBindSession(task_id="t-1", bind_key=BIND_KEY, qr_url="u")

    def test_waiting(self) -> None:
        async def body():
            FakeHttpxClient._response = FakeHttpxResponse(json_data={
                "retcode": 0, "data": {"status": 1},
            })
            with patch("app.adapters.qq_bind.httpx.AsyncClient", FakeHttpxClient):
                result = await poll_bind_result(self._session())
                assert result == {"status": "waiting"}

        _run(body())

    def test_expired(self) -> None:
        async def body():
            FakeHttpxClient._response = FakeHttpxResponse(json_data={
                "retcode": 0, "data": {"status": 3},
            })
            with patch("app.adapters.qq_bind.httpx.AsyncClient", FakeHttpxClient):
                result = await poll_bind_result(self._session())
                assert result == {"status": "expired"}

        _run(body())

    def test_success_with_decrypt(self) -> None:
        async def body():
            encrypted = _make_secret_payload(BIND_KEY, "app-secret-9")
            FakeHttpxClient._response = FakeHttpxResponse(json_data={
                "retcode": 0,
                "data": {
                    "status": 2,
                    "bot_appid": "1024",
                    "bot_encrypt_secret": encrypted,
                },
            })
            with patch("app.adapters.qq_bind.httpx.AsyncClient", FakeHttpxClient):
                result = await poll_bind_result(self._session())
                assert result["status"] == "success"
                assert result["app_id"] == "1024"
                assert result["app_secret"] == "app-secret-9"

        _run(body())

    def test_completed_missing_credentials(self) -> None:
        async def body():
            FakeHttpxClient._response = FakeHttpxResponse(json_data={
                "retcode": 0, "data": {"status": 2},
            })
            with patch("app.adapters.qq_bind.httpx.AsyncClient", FakeHttpxClient):
                result = await poll_bind_result(self._session())
                assert result["status"] == "error"

        _run(body())


class TestAdminQQBindRoutes:
    """Admin QQ 扫码绑定路由。"""

    async def _setup_app(self):
        mongo._db = mongomock_motor.AsyncMongoMockClient()["test_qq_bind"]
        from app.admin.auth import require_admin
        from app.main import app

        app.dependency_overrides[require_admin] = lambda: "admin"
        return app

    def _teardown_app(self, app) -> None:
        app.dependency_overrides.clear()
        mongo._db = None

    def _client(self, app) -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    def test_qrcode_and_poll_flow(self) -> None:
        async def body():
            app = await self._setup_app()
            try:
                async with self._client(app) as client:
                    from app.admin.routes.common import _bot_config_repo
                    from app.models.bot_config import BotConfigCreate

                    bot = await _bot_config_repo.create(BotConfigCreate(
                        account_id="acc-1", name="QQ测试", platform="qq",
                    ))

                    # 获取二维码：mock create_bind_task 网络请求
                    FakeHttpxClient._response = FakeHttpxResponse(json_data={
                        "retcode": 0, "data": {"task_id": "t-9"},
                    })
                    with patch("app.adapters.qq_bind.httpx.AsyncClient", FakeHttpxClient):
                        resp = await client.post(
                            f"/admin/qq-bots/{bot.id}/qrcode"
                        )
                    assert resp.status_code == 200
                    assert "data-bind-task=\"t-9\"" in resp.text

                    # 轮询：等待中
                    FakeHttpxClient._response = FakeHttpxResponse(json_data={
                        "retcode": 0, "data": {"status": 1},
                    })
                    with patch("app.adapters.qq_bind.httpx.AsyncClient", FakeHttpxClient):
                        resp = await client.get(f"/admin/qq-bots/{bot.id}/poll-qr")
                    assert resp.json()["status"] == "waiting"

                    # 轮询：绑定成功 → 凭据写入，验证流程转后台异步执行
                    # （用路由保存的会话 bind_key 构造密文）
                    # 重置节流时间戳，避免被服务端节流拦截
                    from app.admin.routes.qq_bots import _last_poll_at, _pending_sessions
                    _last_poll_at.pop(bot.id, None)
                    session = _pending_sessions[bot.id]
                    encrypted = _make_secret_payload(session.bind_key, "s-1")
                    FakeHttpxClient._response = FakeHttpxResponse(json_data={
                        "retcode": 0,
                        "data": {
                            "status": 2,
                            "bot_appid": "2048",
                            "bot_encrypt_secret": encrypted,
                        },
                    })
                    with patch("app.adapters.qq_bind.httpx.AsyncClient", FakeHttpxClient):
                        resp = await client.get(f"/admin/qq-bots/{bot.id}/poll-qr")
                    assert resp.json()["status"] == "verifying"

                    updated = await _bot_config_repo.find_by_id(bot.id)
                    assert updated.app_id == "2048"
                    assert updated.app_secret == "s-1"
                    # 会话已清理
                    assert bot.id not in _pending_sessions
            finally:
                self._teardown_app(app)

        _run(body())

    def test_poll_without_session(self) -> None:
        async def body():
            app = await self._setup_app()
            try:
                async with self._client(app) as client:
                    resp = await client.get("/admin/qq-bots/none/poll-qr")
                    assert resp.status_code == 404
            finally:
                self._teardown_app(app)

        _run(body())

    async def _create_session(self, client: AsyncClient, bot_id: str) -> None:
        """辅助：创建绑定会话（mock 平台创建任务接口）。"""
        FakeHttpxClient._response = FakeHttpxResponse(json_data={
            "retcode": 0, "data": {"task_id": "t-throttle"},
        })
        with patch("app.adapters.qq_bind.httpx.AsyncClient", FakeHttpxClient):
            resp = await client.post(f"/admin/qq-bots/{bot_id}/qrcode")
        assert resp.status_code == 200

    def test_poll_throttled_without_platform_call(self) -> None:
        """服务端节流：间隔不足时直接返回 waiting，不调用平台接口。"""
        async def body():
            app = await self._setup_app()
            try:
                async with self._client(app) as client:
                    from app.admin.routes.common import _bot_config_repo
                    from app.admin.routes.qq_bots import _pending_sessions
                    from app.models.bot_config import BotConfigCreate

                    bot = await _bot_config_repo.create(BotConfigCreate(
                        account_id="acc-1", name="QQ节流", platform="qq",
                    ))
                    await self._create_session(client, bot.id)

                    # 第一次轮询：正常调用平台（返回 waiting）
                    FakeHttpxClient._response = FakeHttpxResponse(json_data={
                        "retcode": 0, "data": {"status": 1},
                    })
                    with patch("app.adapters.qq_bind.httpx.AsyncClient", FakeHttpxClient):
                        resp = await client.get(f"/admin/qq-bots/{bot.id}/poll-qr")
                    assert resp.json()["status"] == "waiting"

                    # 立即第二次轮询：被节流，不调用平台（不 patch 网络层，若调用会真实请求）
                    resp = await client.get(f"/admin/qq-bots/{bot.id}/poll-qr")
                    assert resp.json()["status"] == "waiting"
                    assert resp.json()["message"] == "等待扫码..."

                    _pending_sessions.pop(bot.id, None)
            finally:
                self._teardown_app(app)

        _run(body())

    def test_poll_rate_limited_degrades(self) -> None:
        """平台限流：返回 rate_limited 并提升该会话最小轮询间隔。"""
        async def body():
            app = await self._setup_app()
            try:
                async with self._client(app) as client:
                    from app.admin.routes.common import _bot_config_repo
                    from app.admin.routes.qq_bots import (
                        _last_poll_at,
                        _pending_sessions,
                        _rate_limited,
                    )
                    from app.models.bot_config import BotConfigCreate

                    bot = await _bot_config_repo.create(BotConfigCreate(
                        account_id="acc-1", name="QQ限流", platform="qq",
                    ))
                    await self._create_session(client, bot.id)

                    # 平台返回限流错误
                    FakeHttpxClient._response = FakeHttpxResponse(json_data={
                        "retcode": 100, "msg": "轮询频率过高，请稍后再试",
                    })
                    with patch("app.adapters.qq_bind.httpx.AsyncClient", FakeHttpxClient):
                        resp = await client.get(f"/admin/qq-bots/{bot.id}/poll-qr")
                    assert resp.json()["status"] == "rate_limited"
                    assert bot.id in _rate_limited

                    # 限流后间隔提升：3 秒后来轮询仍被节流（不调用平台）
                    _last_poll_at[bot.id] -= 3.0
                    resp = await client.get(f"/admin/qq-bots/{bot.id}/poll-qr")
                    assert resp.json()["status"] == "waiting"

                    # 超过 6 秒后可正常调用平台
                    _last_poll_at[bot.id] -= 4.0
                    FakeHttpxClient._response = FakeHttpxResponse(json_data={
                        "retcode": 0, "data": {"status": 1},
                    })
                    with patch("app.adapters.qq_bind.httpx.AsyncClient", FakeHttpxClient):
                        resp = await client.get(f"/admin/qq-bots/{bot.id}/poll-qr")
                    assert resp.json()["status"] == "waiting"

                    _pending_sessions.pop(bot.id, None)
                    _rate_limited.discard(bot.id)
            finally:
                self._teardown_app(app)

        _run(body())
