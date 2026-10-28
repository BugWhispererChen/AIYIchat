"""QQ WebSocket 适配器测试：qq_websocket。

使用 monkeypatch 替换 httpx.AsyncClient，避免真实网络；
用 mock 引擎覆盖消息事件处理与命令分发。
"""

import asyncio
import base64
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import mongomock_motor

from app.adapters.qq_websocket import (
    API_BASE_URL_PROD,
    API_BASE_URL_SANDBOX,
    INTENT_GROUP_AND_C2C,
    INTENT_INTERACTION,
    BotManager,
    QQWebSocketClient,
    _decode_qq_face,
    _QQ_FACE_RE,
)
from app.db import mongo
from app.bot_config import BotConfig


def _run(coro) -> None:
    asyncio.run(coro)


def _make_bot_config(**kwargs) -> BotConfig:
    base = dict(
        id="bot-1", name="测试Bot", app_id="app-1",
        app_secret="secret-1", sandbox_mode=True,
    )
    base.update(kwargs)
    return BotConfig(**base)


def _make_client(**kwargs) -> QQWebSocketClient:
    client = QQWebSocketClient(_make_bot_config(**kwargs))
    client.engine = SimpleNamespace()
    # 输入状态是 fire-and-forget；默认屏蔽，专门用例再恢复真实方法验证协议
    client._send_input_notify = AsyncMock()
    return client


class FakeHttpxResponse:
    """伪造 httpx 响应。"""

    def __init__(
        self, *, json_data=None, status_code=200, text="", json_error=None
    ) -> None:
        self._json = json_data if json_data is not None else {}
        self.status_code = status_code
        self.text = text
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._json


class FakeHttpxClient:
    """伪造 httpx.AsyncClient 异步上下文管理器。"""

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    @classmethod
    def _take(cls):
        if cls._responses:
            return cls._responses.pop(0)
        return cls._response

    async def post(self, *args, **kwargs):
        FakeHttpxClient._requests.append(("POST", args, kwargs))
        return FakeHttpxClient._take()

    async def get(self, *args, **kwargs):
        FakeHttpxClient._requests.append(("GET", args, kwargs))
        return FakeHttpxClient._take()

    async def put(self, *args, **kwargs):
        FakeHttpxClient._requests.append(("PUT", args, kwargs))
        return FakeHttpxClient._take()

    async def delete(self, *args, **kwargs):
        FakeHttpxClient._requests.append(("DELETE", args, kwargs))
        return FakeHttpxClient._take()

    _response = FakeHttpxResponse()
    _responses = []
    _requests = []


class TestQQFaceDecode:
    """QQ 表情标签解析。"""

    def test_decode_face(self) -> None:
        ext = base64.b64encode(json.dumps({"text": "微笑"}).encode()).decode()
        raw = f'<faceType=1,faceId="1",ext="{ext}">'
        decoded = _QQ_FACE_RE.sub(_decode_qq_face, raw)
        assert decoded == "[表情:微笑]"

    def test_decode_face_bad_ext(self) -> None:
        raw = '<faceType=1,faceId="1",ext="not-base64">'
        decoded = _QQ_FACE_RE.sub(_decode_qq_face, raw)
        assert decoded == "[表情]"


class TestTokenAndGateway:
    """access_token 与网关地址获取。"""

    def test_get_access_token_cached(self) -> None:
        async def body():
            client = _make_client()
            FakeHttpxClient._response = FakeHttpxResponse(json_data={
                "access_token": "AT", "expires_in": 7200,
            })
            with patch("app.adapters.qq_websocket.httpx.AsyncClient", FakeHttpxClient):
                tok = await client.get_access_token()
                assert tok == "AT"
                # 第二次命中缓存（无需再次请求）
                assert await client.get_access_token() == "AT"

        _run(body())

    def test_get_access_token_missing(self) -> None:
        async def body():
            client = _make_client()
            FakeHttpxClient._response = FakeHttpxResponse(json_data={"err": "x"})
            with patch("app.adapters.qq_websocket.httpx.AsyncClient", FakeHttpxClient):
                try:
                    await client.get_access_token()
                    raise AssertionError("应抛 RuntimeError")
                except RuntimeError:
                    pass

        _run(body())

    def test_get_access_token_never_logs_credentials(self, caplog) -> None:
        """令牌原文一律不许落日志：只允许字段名与有效期。

        这是本轮修的凭据泄露点位（原实现 `Token response: {整串 JSON}`），
        响应里同时带 access_token 与 refresh_token，二者任一被截走都能直接冒用 Bot。
        """
        async def body():
            client = _make_client()
            FakeHttpxClient._response = FakeHttpxResponse(json_data={
                "access_token": "AT_leak_probe_1234567890",
                "refresh_token": "RT_leak_probe_0987654321",
                "expires_in": 7200,
            })
            with caplog.at_level(logging.INFO, logger="app.adapters.qq_websocket"):
                with patch(
                    "app.adapters.qq_websocket.httpx.AsyncClient", FakeHttpxClient
                ):
                    assert await client.get_access_token() == "AT_leak_probe_1234567890"
            text = caplog.text
            assert "AT_leak_probe_1234567890" not in text
            assert "RT_leak_probe_0987654321" not in text
            # 排查需要的信息仍然在：字段名 + 有效期
            assert "Token 响应字段" in text
            assert "有效期" in text

        _run(body())

    def test_get_access_token_failure_message_has_no_body(self, caplog) -> None:
        """失败分支只报 errcode/errmsg，不把整个响应体塞进异常文本。"""
        async def body():
            client = _make_client()
            FakeHttpxClient._response = FakeHttpxResponse(json_data={
                "errcode": 40001, "errmsg": "invalid app_secret",
            })
            with patch("app.adapters.qq_websocket.httpx.AsyncClient", FakeHttpxClient):
                try:
                    await client.get_access_token()
                    raise AssertionError("应抛 RuntimeError")
                except RuntimeError as exc:
                    assert "40001" in str(exc)
                    assert "invalid app_secret" in str(exc)

        _run(body())

    def test_get_gateway_url(self) -> None:
        async def body():
            client = _make_client()
            client._access_token = "AT"
            client._token_expires_at = 9e18
            FakeHttpxClient._response = FakeHttpxResponse(json_data={
                "url": "wss://gateway",
            })
            with patch("app.adapters.qq_websocket.httpx.AsyncClient", FakeHttpxClient):
                assert await client.get_gateway_url() == "wss://gateway"

        _run(body())


class TestCommandsAndRateLimit:
    """命令处理与限流退避。"""

    def test_handle_command(self) -> None:
        client = _make_client()
        assert "帮助" in client._handle_command("/help")
        assert "未知指令" in client._handle_command("/unknown")

    def test_rate_limit(self) -> None:
        from datetime import datetime, timedelta, timezone

        client = _make_client()
        client._proactive_authorizations[("c2c", "u-1")] = True
        assert client._is_rate_limited("u-1") is False
        assert client.can_send_proactive("u-1") is True
        client._rate_limited_openids["u-1"] = (
            datetime.now(timezone.utc) + timedelta(hours=1)
        )
        assert client._is_rate_limited("u-1") is True
        assert client.can_send_proactive("u-1") is False
        # 已过期自动清除
        client._rate_limited_openids["u-2"] = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        )
        assert client._is_rate_limited("u-2") is False

    def test_check_rate_limit_error(self) -> None:
        client = _make_client()
        # 新 err_code 与旧 code 均按稳定码识别，文案不参与判断
        client._check_rate_limit_error(FakeHttpxResponse(json_data={
            "err_code": 40034122, "message": "任意可变文案",
        }), "u-1")
        client._check_rate_limit_error(FakeHttpxResponse(json_data={
            "code": 40034122, "message": "另一套文案",
        }), "u-legacy")
        assert "u-1" in client._rate_limited_openids
        assert "u-legacy" in client._rate_limited_openids
        # 旧限流文案但非限流码不得触发；HTTP 429 即使非 JSON 也应触发
        client._check_rate_limit_error(FakeHttpxResponse(json_data={
            "code": 40001, "message": "召回消息已达区间上限",
        }), "u-2")
        assert "u-2" not in client._rate_limited_openids
        client._check_rate_limit_error(FakeHttpxResponse(
            status_code=429, json_error=ValueError("not json")
        ), "u-429")
        assert "u-429" in client._rate_limited_openids

    def test_stop_and_close(self) -> None:
        async def body():
            client = _make_client()
            client._running = True
            client.stop()
            assert client._running is False
            await client.close()  # 无 ws 时安全返回

        _run(body())


class TestSendReply:
    """回复发送。"""

    def test_send_reply_success(self) -> None:
        async def body():
            client = _make_client()
            client._access_token = "AT"
            client._token_expires_at = 9e18
            FakeHttpxClient._response = FakeHttpxResponse(status_code=200)
            with patch("app.adapters.qq_websocket.httpx.AsyncClient", FakeHttpxClient):
                ok = await client._send_reply("u-1", "hi", "m-1", False, "")
                assert ok is True

        _run(body())

    def test_send_reply_failure_records_rate_limit(self) -> None:
        async def body():
            client = _make_client()
            client._access_token = "AT"
            client._token_expires_at = 9e18
            FakeHttpxClient._response = FakeHttpxResponse(
                status_code=429,
                json_data={"code": 40034122, "message": "召回消息已达区间上限"},
                text="rate limited",
            )
            with patch("app.adapters.qq_websocket.httpx.AsyncClient", FakeHttpxClient):
                ok = await client._send_reply("u-1", "hi", "m-1", False, "")
                assert ok is False
                assert "u-1" in client._rate_limited_openids

        _run(body())

    def test_send_proactive_blocked_by_rate_limit(self) -> None:
        async def body():
            from datetime import datetime, timedelta, timezone

            client = _make_client()
            client._proactive_authorizations[("c2c", "u-1")] = True
            client._rate_limited_openids["u-1"] = (
                datetime.now(timezone.utc) + timedelta(hours=1)
            )
            assert await client.send_proactive("u-1", "hi") is False
            assert "限流" in client.last_send_error

        _run(body())

    def test_send_proactive_ok(self) -> None:
        async def body():
            client = _make_client()
            client._proactive_authorizations[("c2c", "u-1")] = True
            client._proactive_authorizations[("group", "g-1")] = True
            client._send_reply = AsyncMock(return_value=True)
            assert await client.send_proactive("u-1", "hi") is True
            c2c = client._send_reply.await_args.kwargs
            assert c2c["is_wakeup"] is True

            assert await client.send_proactive(
                "u-1", "hi", is_group=True, group_id="g-1"
            ) is True
            group = client._send_reply.await_args.kwargs
            assert group["is_wakeup"] is False

        _run(body())

    def test_send_proactive_unknown_or_rejected_never_calls_api(self) -> None:
        """未知与明确拒绝都在网络请求前拦截，普通被动回复不走此门槛。"""
        async def body():
            client = _make_client()
            client._send_reply = AsyncMock(return_value=True)
            assert await client.send_proactive("unknown", "hi") is False
            client._proactive_authorizations[("c2c", "rejected")] = False
            assert await client.send_proactive("rejected", "hi") is False
            client._send_reply.assert_not_awaited()
            assert "未授权" in client.last_send_error

        _run(body())

    def test_send_reply_rejects_empty_content(self) -> None:
        """空 content 连请求都不发：平台会以 400/40034030 拒绝，失败只落日志等于已读不回。"""
        async def body():
            client = _make_client()
            client.get_access_token = AsyncMock()
            assert await client._send_reply("u-1", "   ", "m-1", False, "") is False
            client.get_access_token.assert_not_awaited()

        _run(body())


class TestOfficialHTTPContracts:
    """基于 QQ 官方 API 调用指南样例的 HTTP 契约测试。"""

    def test_official_prod_urls_and_send_payload(self) -> None:
        """令牌、Gateway、REST 均使用新基址，请求体保持官方字段。"""
        async def body():
            client = _make_client(sandbox_mode=False)
            FakeHttpxClient._requests = []
            FakeHttpxClient._response = FakeHttpxResponse(json_data={
                "access_token": "ACCESS_TOKEN", "expires_in": "7200",
            })
            with patch("app.adapters.qq_websocket.httpx.AsyncClient", FakeHttpxClient):
                assert await client.get_access_token() == "ACCESS_TOKEN"
                assert FakeHttpxClient._requests[-1][1][0] == (
                    "https://bots.qq.com/app/getAppAccessToken"
                )
                FakeHttpxClient._response = FakeHttpxResponse(json_data={
                    "url": "wss://api.bot.qq.com/websocket/",
                })
                assert await client.get_gateway_url() == (
                    "wss://api.bot.qq.com/websocket/"
                )
                assert FakeHttpxClient._requests[-1][1][0] == (
                    f"{API_BASE_URL_PROD}/gateway"
                )
                FakeHttpxClient._response = FakeHttpxResponse(json_data={
                    "id": "ROBOT1.0_xxx", "timestamp": "2026-07-21T10:30:00+08:00",
                })
                assert await client._send_reply(
                    "A1B2C3D4", "Hello World", "previous_msg_id", False, ""
                ) is True
                method, args, kwargs = FakeHttpxClient._requests[-1]
                assert method == "POST"
                assert args[0] == (
                    f"{API_BASE_URL_PROD}/v2/users/A1B2C3D4/messages"
                )
                assert kwargs["json"]["content"] == "Hello World"
                assert kwargs["json"]["msg_type"] == 0
                assert kwargs["json"]["msg_id"] == "previous_msg_id"

        _run(body())

    def test_sandbox_base_remains_isolated(self) -> None:
        """沙箱消息请求不得随正式基址迁移。"""
        async def body():
            client = _make_client(sandbox_mode=True)
            client._access_token = "AT"
            client._token_expires_at = 9e18
            FakeHttpxClient._requests = []
            FakeHttpxClient._response = FakeHttpxResponse(json_data={"id": "m"})
            with patch("app.adapters.qq_websocket.httpx.AsyncClient", FakeHttpxClient):
                assert await client._send_reply("u-1", "hi", "m-1", False, "")
            assert FakeHttpxClient._requests[-1][1][0].startswith(
                f"{API_BASE_URL_SANDBOX}/"
            )

        _run(body())

    def test_err_code_trace_id_and_auth_body_redaction(self, caplog) -> None:
        """官方失败样例保留 err_code/trace_id，完整鉴权响应与令牌不落日志。"""
        async def body():
            client = _make_client(app_secret="SECRET_LEAK_123")
            client._access_token = "TOKEN_LEAK_123"
            client._token_expires_at = 9e18
            FakeHttpxClient._response = FakeHttpxResponse(
                status_code=400,
                json_data={
                    "err_code": 40034005,
                    "message": "回复消息msg_id已过期",
                    "trace_id": "4a8a61565b909f199b1ec169fdd6f49e",
                    "access_token": "TOKEN_LEAK_123",
                    "refresh_token": "REFRESH_LEAK_123",
                },
                text="完整响应不得直接记录",
            )
            with caplog.at_level(logging.ERROR, logger="app.adapters.qq_websocket"):
                with patch("app.adapters.qq_websocket.httpx.AsyncClient", FakeHttpxClient):
                    assert await client._send_reply("u-1", "hi", "m-1", False, "") is False
            assert "40034005" in client.last_send_error
            assert "4a8a61565b909f199b1ec169fdd6f49e" in client.last_send_error
            for leaked in ("TOKEN_LEAK_123", "REFRESH_LEAK_123", "SECRET_LEAK_123"):
                assert leaked not in caplog.text
                assert leaked not in client.last_send_error
            assert "完整响应不得直接记录" not in caplog.text

        _run(body())

    def test_legacy_code_and_http_200_business_error(self) -> None:
        """旧 code/message 过渡兼容，HTTP 200 的 err_code 也必须发送失败。"""
        async def body():
            client = _make_client()
            client._access_token = "AT"
            client._token_expires_at = 9e18
            with patch("app.adapters.qq_websocket.httpx.AsyncClient", FakeHttpxClient):
                FakeHttpxClient._response = FakeHttpxResponse(
                    status_code=400,
                    json_data={"code": 40034122, "message": "legacy"},
                )
                assert await client._send_reply("u-1", "hi", "m-1", False, "") is False
                assert "40034122" in client.last_send_error
                assert "legacy" in client.last_send_error
                FakeHttpxClient._response = FakeHttpxResponse(
                    status_code=200,
                    json_data={"err_code": 40034005, "message": "expired"},
                )
                assert await client._send_reply("u-1", "hi", "m-1", False, "") is False

        _run(body())

    def test_json_and_network_failures_return_false(self) -> None:
        """成功状态的非法 JSON 与网络异常统一转为安全失败。"""
        async def body():
            client = _make_client()
            client._access_token = "AT"
            client._token_expires_at = 9e18
            FakeHttpxClient._response = FakeHttpxResponse(
                status_code=200, json_error=ValueError("bad json")
            )
            with patch("app.adapters.qq_websocket.httpx.AsyncClient", FakeHttpxClient):
                assert await client._send_reply("u-1", "hi", "m-1", False, "") is False
                assert "JSON" in client.last_send_error
            with patch(
                "app.adapters.qq_websocket.httpx.AsyncClient",
                side_effect=httpx.ConnectError("connection failed"),
            ):
                assert await client._send_reply("u-1", "hi", "m-1", False, "") is False
                assert "connection failed" in client.last_send_error

        _run(body())

    def test_identify_subscribes_interaction_events(self) -> None:
        """授权互动事件需要 1<<26，不能只订阅消息事件。"""
        async def body():
            client = _make_client()
            client._ws = SimpleNamespace(send=AsyncMock())
            client.get_access_token = AsyncMock(return_value="AT")
            await client._send_identify()
            identify = json.loads(client._ws.send.await_args.args[0])
            assert identify["d"]["intents"] == (
                INTENT_GROUP_AND_C2C | INTENT_INTERACTION
            )

        _run(body())


class TestMessageEvent:
    """消息事件处理（mock 引擎）。"""

    def test_c2c_text_message(self) -> None:
        async def body():
            client = _make_client()
            client.engine.handle_message = AsyncMock(return_value=SimpleNamespace(
                text="回复内容", texts=[], image_tasks=[], conversation_id="c-1",
                voice_id="v", voice_instruction="", voice_pitch_rate=1.0,
                voice_emotion_tag="", account_id="acc-1",
            ))
            client._send_reply = AsyncMock()
            data = {
                "id": "m-1",
                "author": {"user_openid": "u-1", "username": "小明"},
                "content": "你好",
                "attachments": [],
            }
            # 未配置任何主动授权时，普通 C2C 被动回复仍按官方 Dispatch 处理
            await client._handle_dispatch({"t": "C2C_MESSAGE_CREATE", "d": data})
            client.engine.handle_message.assert_awaited()
            client._send_reply.assert_awaited()

        _run(body())

    def test_empty_reply_sends_no_text(self) -> None:
        """整轮为空（兜底开关关闭时）：不发文本，空 content 会被平台 400 拒绝。"""
        async def body():
            client = _make_client()
            client.engine.handle_message = AsyncMock(return_value=SimpleNamespace(
                text="", texts=[], image_tasks=[], conversation_id="c-1",
                voice_id="v", voice_instruction="", voice_pitch_rate=1.0,
                voice_emotion_tag="", account_id="acc-1",
            ))
            client._send_reply = AsyncMock()
            data = {
                "id": "m-1",
                "author": {"user_openid": "u-1", "username": "小明"},
                "content": "你好",
                "attachments": [],
            }
            await client._handle_message_event("C2C_MESSAGE_CREATE", data)
            client.engine.handle_message.assert_awaited()
            client._send_reply.assert_not_awaited()

        _run(body())

    def test_multi_message_reply_sent_separately(self) -> None:
        """多消息回复：texts 非空时逐条发送，且消息间有打字延迟。"""
        async def body():
            from app.config import settings

            client = _make_client()
            client.engine.handle_message = AsyncMock(return_value=SimpleNamespace(
                text="第一条\n第二条\n第三条",
                texts=["第一条", "第二条", "第三条"],
                image_tasks=[], conversation_id="c-1",
                voice_id="v", voice_instruction="", voice_pitch_rate=1.0,
                voice_emotion_tag="", account_id="acc-1",
            ))
            client._send_reply = AsyncMock()
            data = {
                "id": "m-multi",
                "author": {"user_openid": "u-1", "username": "小明"},
                "content": "讲讲你的看法", "attachments": [],
            }
            with patch.object(settings, "multi_message_enabled", True), patch(
                "app.adapters.qq_websocket.asyncio.sleep", new=AsyncMock()
            ) as mock_sleep:
                await client._handle_message_event("C2C_MESSAGE_CREATE", data)

            # 逐条发送 3 次，内容与拆分列表一致
            assert client._send_reply.await_count == 3
            sent = [c.kwargs.get("content") or c.args[1]
                    for c in client._send_reply.await_args_list]
            assert sent == ["第一条", "第二条", "第三条"]
            # 首条无延迟，后续每条插入打字延迟
            assert mock_sleep.await_count == 2

        _run(body())

    def test_multi_message_disabled_falls_back_single(self) -> None:
        """总开关关闭时回退单条完整文本。"""
        async def body():
            from app.config import settings

            client = _make_client()
            client.engine.handle_message = AsyncMock(return_value=SimpleNamespace(
                text="完整回复",
                texts=["完整回复"],
                image_tasks=[], conversation_id="c-1",
                voice_id="v", voice_instruction="", voice_pitch_rate=1.0,
                voice_emotion_tag="", account_id="acc-1",
            ))
            client._send_reply = AsyncMock()
            data = {
                "id": "m-single",
                "author": {"user_openid": "u-1", "username": "小明"},
                "content": "在吗", "attachments": [],
            }
            with patch.object(settings, "multi_message_enabled", False):
                await client._handle_message_event("C2C_MESSAGE_CREATE", data)

            assert client._send_reply.await_count == 1

        _run(body())

    def test_duplicate_message_skipped(self) -> None:
        async def body():
            client = _make_client()
            client.engine.handle_message = AsyncMock()
            client._send_reply = AsyncMock()
            data = {
                "id": "dup-1",
                "author": {"user_openid": "u-1", "username": "小明"},
                "content": "你好", "attachments": [],
            }
            await client._handle_message_event("C2C_MESSAGE_CREATE", data)
            # 第二次相同 id 被去重跳过
            await client._handle_message_event("C2C_MESSAGE_CREATE", data)
            assert client.engine.handle_message.await_count == 1

        _run(body())

    def test_command_message(self) -> None:
        async def body():
            client = _make_client()
            client._send_reply = AsyncMock()
            data = {
                "id": "m-2",
                "author": {"user_openid": "u-1", "username": "小明"},
                "content": "/help", "attachments": [],
            }
            await client._handle_message_event("C2C_MESSAGE_CREATE", data)
            client._send_reply.assert_awaited()

        _run(body())

    def test_inbound_image_and_voice_attachments(self) -> None:
        async def body():
            from app.config import settings

            client = _make_client()
            result = SimpleNamespace(
                text="收到", texts=[], image_tasks=[], conversation_id="c-1",
                voice_id="v", voice_instruction="", voice_pitch_rate=1.0,
                voice_emotion_tag="", account_id="acc-1",
            )
            client.engine.handle_message = AsyncMock(return_value=result)
            client._send_reply = AsyncMock()
            FakeHttpxClient._responses = []
            FakeHttpxClient._response = FakeHttpxResponse(status_code=404)
            with patch.object(settings, "vision_enabled", False), patch(
                "app.services.media_storage.save_image_from_url",
                AsyncMock(return_value="/api/media/img-1"),
            ), patch(
                "app.adapters.qq_websocket.httpx.AsyncClient", FakeHttpxClient
            ):
                await client._handle_message_event("C2C_MESSAGE_CREATE", {
                    "id": "img-msg",
                    "author": {"user_openid": "u-1", "username": "小明"},
                    "content": "",
                    "attachments": [{
                        "content_type": "image/png",
                        "url": "https://example.test/a.png",
                    }],
                })
            image_kwargs = client.engine.handle_message.await_args.kwargs
            assert image_kwargs["content_type"] == "image"
            assert image_kwargs["media_url"] == "/api/media/img-1"

            client.engine.handle_message = AsyncMock(return_value=result)
            with patch.object(settings, "tts_enabled", False):
                await client._handle_message_event("C2C_MESSAGE_CREATE", {
                    "id": "voice-msg",
                    "author": {"user_openid": "u-1", "username": "小明"},
                    "content": "",
                    "attachments": [{
                        "content_type": "voice",
                        "voice_wav_url": "https://example.test/a.wav",
                        "asr_refer_text": "语音里的文字",
                    }],
                })
            voice_kwargs = client.engine.handle_message.await_args.kwargs
            assert voice_kwargs["text"] == "语音里的文字"
            assert voice_kwargs["content_type"] == "voice"

        _run(body())

    def test_dispatch_ready_and_resumed(self) -> None:
        async def body():
            client = _make_client()
            await client._handle_dispatch({
                "t": "READY", "d": {"session_id": "s-1", "user": {"username": "bot"}},
            })
            await client._handle_dispatch({"t": "RESUMED", "d": {}})

        _run(body())


class TestProactiveAuthorizationContracts:
    """基于官方授权事件样例的持久化、恢复与双闸门测试。"""

    def test_c2c_event_restore_bot_isolation_and_llm_precheck(self) -> None:
        """C2C 授权落库后可跨实例恢复，其他 Bot 与拒绝状态不可发送。"""
        async def body():
            from app.proactive.scheduler import ProactiveScheduler

            old_db = mongo._db
            mongo._db = mongomock_motor.AsyncMongoMockClient()["test_qq_auth"]
            try:
                client = _make_client(id="bot-auth")
                await client._handle_dispatch({
                    "t": "C2C_MSG_RECEIVE",
                    "d": {
                        "openid": "E4F4AEA33253A2797FB897C50B81D7ED",
                        "timestamp": 1699240617,
                    },
                })
                restored = _make_client(id="bot-auth")
                await restored._restore_proactive_authorizations()
                openid = "E4F4AEA33253A2797FB897C50B81D7ED"
                assert restored.can_send_proactive(openid) is True

                other_bot = _make_client(id="bot-other")
                await other_bot._restore_proactive_authorizations()
                assert other_bot.can_send_proactive(openid) is False

                scheduler = ProactiveScheduler()
                scheduler._qq_manager = SimpleNamespace(
                    get_client=lambda bot_id: other_bot
                )
                scheduler._generate_content = AsyncMock()
                assert scheduler._can_send("qq", "bot-other", openid) is False
                scheduler._generate_content.assert_not_called()

                await restored._handle_dispatch({
                    "t": "C2C_MSG_REJECT",
                    "d": {"openid": openid, "timestamp": 1699240599},
                })
                assert restored.can_send_proactive(openid) is False
                restored._send_reply = AsyncMock(return_value=True)
                assert await restored.send_proactive(openid, "hi") is False
                restored._send_reply.assert_not_awaited()
            finally:
                mongo._db = old_db

        _run(body())

    def test_group_receive_reject_and_composite_precheck(self) -> None:
        """群授权按 group_openid 恢复，成员联合键不会误用 C2C 授权。"""
        async def body():
            old_db = mongo._db
            mongo._db = mongomock_motor.AsyncMongoMockClient()["test_qq_group_auth"]
            try:
                client = _make_client()
                group_id = "C9F778FE6ADF9D1D1DBE395BF744A33A"
                member_id = "E4F4AEA33253A2797FB897C50B81D7ED"
                await client._handle_dispatch({
                    "t": "GROUP_MSG_RECEIVE",
                    "d": {
                        "group_openid": group_id,
                        "op_member_openid": member_id,
                        "timestamp": 1699240477,
                    },
                })
                assert client.can_send_proactive(f"{group_id}::{member_id}") is True
                assert client.can_send_proactive(member_id) is False
                restored = _make_client()
                await restored._restore_proactive_authorizations()
                assert restored.can_send_proactive(f"{group_id}::{member_id}") is True

                await restored._handle_dispatch({
                    "t": "GROUP_MSG_REJECT",
                    "d": {
                        "group_openid": group_id,
                        "op_member_openid": member_id,
                        "timestamp": 1699240458,
                    },
                })
                assert restored.can_send_proactive(f"{group_id}::{member_id}") is False
            finally:
                mongo._db = old_db

        _run(body())

    def test_interaction_create_authorization_official_shape(self) -> None:
        """type=18 的 resolved.authorize_data 官方嵌套结构可授权 C2C。"""
        async def body():
            old_db = mongo._db
            mongo._db = mongomock_motor.AsyncMongoMockClient()["test_qq_interaction"]
            try:
                client = _make_client()
                openid = "A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4"
                await client._handle_dispatch({
                    "t": "INTERACTION_CREATE",
                    "d": {
                        "id": "1b13d569-4610-4ab9-bc51-feecc5def6d4",
                        "type": 18,
                        "scene": "c2c",
                        "chat_type": 2,
                        "user_openid": openid,
                        "data": {
                            "resolved": {
                                "authorize_data": {
                                    "opt_scene": "setting", "scope": "c2c_push",
                                },
                            },
                        },
                    },
                })
                assert client.can_send_proactive(openid) is True
                # type=19 + group_push 是群主动授权的对应结构
                await client._handle_dispatch({
                    "t": "INTERACTION_CREATE",
                    "d": {
                        "type": 19,
                        "scene": "group",
                        "group_openid": "group-openid",
                        "data": {
                            "resolved": {
                                "authorize_data": {
                                    "opt_scene": "dialog", "scope": "group_push",
                                },
                            },
                        },
                    },
                })
                assert client.can_send_proactive("group-openid::member") is True
                # 非授权互动不得改变未知状态
                await client._handle_dispatch({
                    "t": "INTERACTION_CREATE",
                    "d": {"type": 11, "user_openid": "other", "data": {}},
                })
                assert client.can_send_proactive("other") is False
            finally:
                mongo._db = old_db

        _run(body())


class TestBotManager:
    """QQ BotManager 生命周期。"""

    def test_start_stop_and_diff(self) -> None:
        async def body():
            mgr = BotManager(poll_interval=1)
            # patch client.run 使其立即返回，避免真实网络
            with patch.object(QQWebSocketClient, "run", new=AsyncMock()):
                await mgr._start_bot(_make_bot_config(id="b-1"))
                assert mgr.get_client("b-1") is not None
                assert len(mgr.list_clients()) == 1

                # apply_diff 新增另一个 bot
                await mgr._apply_diff([
                    _make_bot_config(id="b-1"),
                    _make_bot_config(id="b-2"),
                ])
                assert mgr.get_client("b-2") is not None

                # apply_diff 移除 b-1
                await mgr._apply_diff([_make_bot_config(id="b-2")])
                assert mgr.get_client("b-1") is None

                # 配置变更触发重启
                await mgr._apply_diff([
                    _make_bot_config(id="b-2", name="改名"),
                ])
                assert mgr.get_client("b-2") is not None

                await mgr.shutdown()
                assert mgr.get_client("b-2") is None

        _run(body())

    def test_get_client_missing(self) -> None:
        mgr = BotManager()
        assert mgr.get_client("none") is None
        assert mgr.list_clients() == []


class TestP1InteractionAndContext:
    """P1：Markdown/键盘、原生反馈与全量群上下文。"""

    def test_markdown_keyboard_and_message_binding(self) -> None:
        async def body():
            from app.config import settings
            from app.db.mongo import get_database

            old_db = mongo._db
            mongo._db = mongomock_motor.AsyncMongoMockClient()["test_qq_p1"]
            try:
                client = _make_client()
                client._access_token = "AT"
                client._token_expires_at = 9e18
                FakeHttpxClient._responses = [
                    FakeHttpxResponse(json_data={"id": "srv-msg-1"})
                ]
                with patch.object(settings, "qq_markdown_enabled", True), patch.object(
                    settings, "qq_keyboard_enabled", True
                ), patch(
                    "app.adapters.qq_websocket.httpx.AsyncClient", FakeHttpxClient
                ):
                    assert await client._send_reply(
                        "u-1", "**你好**", "m-1", False, "",
                        use_markdown=True,
                        attach_keyboard=True,
                        conversation_id="conv-1",
                    )
                payload = FakeHttpxClient._requests[-1][2]["json"]
                assert payload["msg_type"] == 2
                assert payload["markdown"]["content"] == "**你好**"
                assert payload["keyboard"]["content"]["rows"][0]["buttons"]
                binding = await get_database().qq_message_bindings.find_one({
                    "bot_id": "bot-1", "platform_message_id": "srv-msg-1",
                })
                assert binding and binding["conversation_id"] == "conv-1"
            finally:
                mongo._db = old_db

        _run(body())

    def test_markdown_permission_failure_falls_back_plain(self) -> None:
        async def body():
            from app.config import settings

            client = _make_client()
            client._access_token = "AT"
            client._token_expires_at = 9e18
            FakeHttpxClient._responses = [
                FakeHttpxResponse(
                    status_code=403,
                    json_data={"err_code": 40054005, "message": "no markdown"},
                ),
                FakeHttpxResponse(json_data={"id": "plain-1"}),
            ]
            with patch.object(settings, "qq_markdown_enabled", True), patch(
                "app.adapters.qq_websocket.httpx.AsyncClient", FakeHttpxClient
            ):
                assert await client._send_reply(
                    "u-1", "你好", "m-1", False, "", use_markdown=True
                )
            assert "markdown" in FakeHttpxClient._requests[-2][2]["json"]
            assert FakeHttpxClient._requests[-1][2]["json"]["content"] == "你好"

        _run(body())

    def test_group_full_messages_are_context_only(self) -> None:
        async def body():
            from app.config import settings

            client = _make_client()
            client.engine.record_context_message = AsyncMock(return_value=True)
            event = {
                "id": "g-msg-1",
                "group_openid": "g-1",
                "author": {"member_openid": "u-1", "username": "小明"},
                "content": "群里的闲聊",
            }
            with patch.object(settings, "qq_full_group_context_enabled", False):
                await client._handle_dispatch({
                    "t": "GROUP_MESSAGE_CREATE", "d": event,
                })
            client.engine.record_context_message.assert_not_awaited()

            with patch.object(settings, "qq_full_group_context_enabled", True):
                await client._handle_dispatch({
                    "t": "GROUP_MESSAGE_CREATE", "d": event,
                })
                await client._handle_dispatch({
                    "t": "GROUP_MESSAGE_CREATE",
                    "d": {**event, "id": "g-msg-2", "content": "<@!bot> 你好"},
                })
            assert client.engine.record_context_message.await_count == 1

        _run(body())

    def test_like_unlike_upsert_is_idempotent_and_mapped(self) -> None:
        async def body():
            from app.db.repositories.clone_repo import CloneFeedbackRepository
            from app.models.clone import CloneFeedbackInDB

            old_db = mongo._db
            mongo._db = mongomock_motor.AsyncMongoMockClient()["test_qq_feedback"]
            try:
                repo = CloneFeedbackRepository()
                feedback = CloneFeedbackInDB(
                    account_id="a-1", service_id="s-1",
                    conversation_id="c-1", message_id="m-internal",
                    rating="like", source="qq",
                    platform_event_id="bot-1:event-1",
                    platform_message_id="srv-msg-1",
                )
                await repo.upsert_platform_event(feedback)
                feedback.rating = "dislike"
                await repo.upsert_platform_event(feedback)
                docs = await mongo._db.clone_feedback.find({}).to_list(None)
                assert len(docs) == 1
                assert docs[0]["rating"] == "dislike"
            finally:
                mongo._db = old_db

        _run(body())

    def test_interaction_feedback_resolves_service_and_message(self) -> None:
        async def body():
            from app.db.repositories.clone_repo import CloneFeedbackRepository
            from app.db.repositories.conversation_repo import ConversationRepository
            from app.db.repositories.message_repo import MessageRepository
            from app.db.repositories.profile_repo import ProfileRepository
            from app.db.repositories.service_repo import ServiceRepository
            from app.db.mongo import get_database

            old_db = mongo._db
            mongo._db = mongomock_motor.AsyncMongoMockClient()["test_qq_feedback_flow"]
            try:
                client = _make_client()
                await get_database().qq_message_bindings.insert_one({
                    "bot_id": "bot-1",
                    "platform_message_id": "srv-msg-9",
                    "conversation_id": "conv-9",
                })
                with patch.object(
                    ConversationRepository, "find_by_id",
                    AsyncMock(return_value=SimpleNamespace(service_id="svc-9")),
                ), patch.object(
                    ServiceRepository, "find_by_id",
                    AsyncMock(return_value=SimpleNamespace(
                        id="svc-9", account_id="acc-9", profile_id="p-9",
                    )),
                ), patch.object(
                    ProfileRepository, "find_by_id",
                    AsyncMock(return_value=SimpleNamespace(
                        id="p-9", clone_version=4,
                    )),
                ), patch.object(
                    MessageRepository, "get_recent",
                    AsyncMock(return_value=[
                        SimpleNamespace(id="internal-9", role="assistant"),
                    ]),
                ), patch.object(
                    CloneFeedbackRepository, "upsert_platform_event",
                    new_callable=AsyncMock,
                ) as upsert:
                    await client._handle_dispatch({
                        "t": "INTERACTION_CREATE",
                        "d": {
                            "id": "event-9",
                            "type": 13,
                            "user_openid": "u-9",
                            "data": {"resolved": {
                                "message_id": "srv-msg-9",
                                "feedback_opt": "LIKE",
                            }},
                        },
                    })
                saved = upsert.await_args.args[0]
                assert saved.rating == "like"
                assert saved.account_id == "acc-9"
                assert saved.message_id == "internal-9"
                assert saved.clone_version == 4
                assert saved.platform_event_id == "bot-1:event-9"
            finally:
                mongo._db = old_db

        _run(body())


class TestP2StreamMediaRecall:
    """P2：流式契约、降级、输入状态、撤回与分片媒体。"""

    def test_input_state_uses_official_msg_type_6(self) -> None:
        async def body():
            client = QQWebSocketClient(_make_bot_config())
            client._access_token = "AT"
            client._token_expires_at = 9e18
            FakeHttpxClient._requests = []
            FakeHttpxClient._responses = [
                FakeHttpxResponse(json_data={"id": "input-msg"})
            ]
            with patch(
                "app.adapters.qq_websocket.httpx.AsyncClient", FakeHttpxClient
            ):
                await client._send_input_notify("u-1", "m-1")
            payload = FakeHttpxClient._requests[-1][2]["json"]
            assert payload["msg_type"] == 6
            assert payload["input_notify"] == {
                "input_type": 1, "input_second": 60,
            }
            assert payload["msg_id"] == "m-1"

        _run(body())

    def test_stream_frame_contract(self) -> None:
        async def body():
            client = _make_client()
            client._access_token = "AT"
            client._token_expires_at = 9e18
            FakeHttpxClient._requests = []
            FakeHttpxClient._responses = [
                FakeHttpxResponse(json_data={"id": "stream-1"})
            ]
            with patch(
                "app.adapters.qq_websocket.httpx.AsyncClient", FakeHttpxClient
            ):
                result = await client._send_stream_frame(
                    "u-1", "in-1", "in-1", 55, 0, "你好", 1,
                )
            assert result == (1, "stream-1")
            url = FakeHttpxClient._requests[-1][1][0]
            payload = FakeHttpxClient._requests[-1][2]["json"]
            assert url.endswith("/v2/users/u-1/stream_messages")
            assert payload["input_mode"] == "replace"
            assert payload["content_type"] == "markdown"
            assert payload["event_id"] == "in-1"
            assert payload["msg_seq"] == 55

        _run(body())

    def test_stream_rate_limit_retries_with_advanced_index(self) -> None:
        async def body():
            client = _make_client()
            client._access_token = "AT"
            client._token_expires_at = 9e18
            FakeHttpxClient._requests = []
            FakeHttpxClient._responses = [
                FakeHttpxResponse(
                    status_code=429,
                    json_data={"err_code": 50002, "message": "rate limit"},
                ),
                FakeHttpxResponse(json_data={"id": "stream-ok"}),
            ]
            with patch(
                "app.adapters.qq_websocket.httpx.AsyncClient", FakeHttpxClient
            ), patch(
                "app.adapters.qq_websocket.asyncio.sleep", new=AsyncMock()
            ):
                frame = await client._send_stream_frame(
                    "u-1", "in-1", "in-1", 77, 0, "你", 1,
                )
            assert frame == (2, "stream-ok")
            assert FakeHttpxClient._requests[0][2]["json"]["index"] == 0
            assert FakeHttpxClient._requests[1][2]["json"]["index"] == 1

        _run(body())

    def test_tts_only_blocks_stream_inside_voice_window(self) -> None:
        async def body():
            from app.config import settings

            client = _make_client()
            client.engine.handle_message = AsyncMock(return_value=SimpleNamespace(
                text="普通回复", texts=[], image_tasks=[], conversation_id="c-1",
                voice_id="v", voice_instruction="", voice_pitch_rate=1.0,
                voice_emotion_tag="", account_id="acc-1",
            ))
            client._handle_stream_turn = AsyncMock()
            client._send_reply = AsyncMock()
            client._latest_conversation_id = AsyncMock(return_value="c-voice")
            data = {
                "id": "stream-tts-1",
                "author": {"user_openid": "u-1", "username": "小明"},
                "content": "文本消息", "attachments": [],
            }
            with patch.object(settings, "qq_stream_enabled", True), patch.object(
                settings, "tts_enabled", True
            ), patch(
                "app.adapters.qq_websocket.voice_window.is_active",
                return_value=True,
            ), patch(
                "app.adapters.qq_websocket.spawn", lambda coro: coro.close()
            ):
                await client._handle_message_event("C2C_MESSAGE_CREATE", data)
            client._handle_stream_turn.assert_not_awaited()
            client.engine.handle_message.assert_awaited()

            with patch.object(settings, "qq_stream_enabled", True), patch.object(
                settings, "tts_enabled", True
            ), patch(
                "app.adapters.qq_websocket.voice_window.is_active",
                return_value=False,
            ), patch(
                "app.adapters.qq_websocket.spawn", lambda coro: coro.close()
            ):
                await client._handle_message_event(
                    "C2C_MESSAGE_CREATE", {**data, "id": "stream-tts-2"}
                )
            client._handle_stream_turn.assert_awaited_once()

        _run(body())

    def test_stream_cancel_closes_platform_bubble(self) -> None:
        async def body():
            client = _make_client()
            started = asyncio.Event()

            async def slow_stream(**_kwargs):
                yield "你好"
                started.set()
                await asyncio.sleep(60)

            client.engine.handle_message_stream = slow_stream
            client._send_stream_frame = AsyncMock(return_value=(1, "stream-1"))
            task = asyncio.create_task(client._handle_stream_turn(
                user_id="u-1", content="hi", nickname="小明",
                msg_id="m-1", local_media_url="", content_type="text",
            ))
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("取消必须继续向上传播")
            final_calls = [
                call for call in client._send_stream_frame.await_args_list
                if len(call.args) > 6 and call.args[6] == 10
            ]
            assert final_calls
            assert final_calls[-1].args[5] == "你好"

        _run(body())

    def test_stream_timeout_retries_then_returns_failure(self) -> None:
        async def body():
            client = _make_client()
            client._access_token = "AT"
            client._token_expires_at = 9e18
            with patch(
                "app.adapters.qq_websocket.httpx.AsyncClient",
                side_effect=httpx.ConnectTimeout("timeout"),
            ), patch(
                "app.adapters.qq_websocket.asyncio.sleep", new=AsyncMock()
            ):
                frame = await client._send_stream_frame(
                    "u-1", "in-1", "in-1", 88, 0, "你", 1,
                )
            assert frame is None

        _run(body())

    def test_stream_failure_degrades_without_rerun(self) -> None:
        async def body():
            client = _make_client()

            async def broken_stream(**_kwargs):
                yield "你"
                yield "好"
                raise RuntimeError("stream disconnected")

            client.engine.handle_message_stream = broken_stream
            client._send_stream_frame = AsyncMock(return_value=None)
            client._send_reply = AsyncMock(return_value=True)
            await client._handle_stream_turn(
                user_id="u-1", content="hi", nickname="小明",
                msg_id="m-1", local_media_url="", content_type="text",
            )
            client.engine.handle_message_stream = broken_stream
            # 只在降级出口发送累计全文，不会再次调用引擎
            client._send_reply.assert_awaited_once()
            assert client._send_reply.await_args.args[1] == "你好"

        _run(body())

    def test_recall_time_boundary(self) -> None:
        async def body():
            import time as time_module

            client = _make_client()
            client._access_token = "AT"
            client._token_expires_at = 9e18
            client._last_sent_by_target["user:u-1"] = (
                "m-1", time_module.time(),
            )
            FakeHttpxClient._requests = []
            FakeHttpxClient._responses = [
                FakeHttpxResponse(status_code=204, json_data={})
            ]
            with patch(
                "app.adapters.qq_websocket.httpx.AsyncClient", FakeHttpxClient
            ):
                assert await client.recall_last_message("u-1") is True
            assert FakeHttpxClient._requests[-1][0] == "DELETE"
            assert FakeHttpxClient._requests[-1][1][0].endswith(
                "/v2/users/u-1/messages/m-1"
            )

            client._last_sent_by_target["user:u-1"] = (
                "old", time_module.time() - 121,
            )
            before = len(FakeHttpxClient._requests)
            assert await client.recall_last_message("u-1") is False
            assert len(FakeHttpxClient._requests) == before

            # 另一会话的最近消息不能被当前会话 /recall 撤回
            client._last_sent_by_target["user:u-2"] = (
                "other-chat", time_module.time(),
            )
            assert await client.recall_last_message("u-1") is False
            assert len(FakeHttpxClient._requests) == before

        _run(body())

    def test_chunked_upload_contract_and_retry(self) -> None:
        async def body():
            from app.adapters.qq_websocket import MEDIA_CHUNK_THRESHOLD

            client = _make_client()
            client._access_token = "AT"
            client._token_expires_at = 9e18
            data = b"x" * (MEDIA_CHUNK_THRESHOLD + 1)
            FakeHttpxClient._requests = []
            FakeHttpxClient._responses = [
                FakeHttpxResponse(json_data={
                    "upload_id": "up-1",
                    "block_size": len(data),
                    "parts": [{
                        "index": 1,
                        "presigned_url": "https://upload.example/part-1",
                    }],
                }),
                FakeHttpxResponse(status_code=500, json_data={}),
                FakeHttpxResponse(status_code=200, json_data={}),
                FakeHttpxResponse(json_data={}),
                FakeHttpxResponse(json_data={"file_info": "FILE_INFO"}),
            ]
            with patch(
                "app.adapters.qq_websocket.httpx.AsyncClient", FakeHttpxClient
            ), patch(
                "app.adapters.qq_websocket.asyncio.sleep", new=AsyncMock()
            ):
                file_info = await client._upload_media_chunked(
                    "u-1", data, 4, False, "", "报告.zip"
                )
            assert file_info == "FILE_INFO"
            methods = [item[0] for item in FakeHttpxClient._requests]
            assert methods == ["POST", "PUT", "PUT", "POST", "POST"]
            prepare = FakeHttpxClient._requests[0][2]["json"]
            assert prepare["file_name"] == "报告.zip"
            assert prepare["md5_10m"] == prepare["md5"]
            finish = FakeHttpxClient._requests[3][2]["json"]
            assert finish["part_index"] == 1

        _run(body())

    def test_media_size_limit_is_enforced_before_network(self) -> None:
        async def body():
            from app.adapters import qq_websocket

            client = _make_client()
            FakeHttpxClient._requests = []
            with patch.object(qq_websocket, "MEDIA_MAX_SIZE", 3):
                result = await client._upload_media_chunked(
                    "u-1", b"1234", 4, False, "", "too-large.bin"
                )
            assert result is None
            assert "100MB" in client.last_send_error
            assert FakeHttpxClient._requests == []

        _run(body())

    def test_keyboard_permission_failure_falls_back_without_keyboard(self) -> None:
        async def body():
            from app.config import settings

            client = _make_client()
            client._access_token = "AT"
            client._token_expires_at = 9e18
            FakeHttpxClient._requests = []
            FakeHttpxClient._responses = [
                FakeHttpxResponse(
                    status_code=403,
                    json_data={"err_code": 40054002, "message": "no keyboard"},
                ),
                FakeHttpxResponse(json_data={"id": "plain-2"}),
            ]
            with patch.object(settings, "qq_keyboard_enabled", True), patch(
                "app.adapters.qq_websocket.httpx.AsyncClient", FakeHttpxClient
            ):
                assert await client._send_reply(
                    "u-1", "帮助", "m-1", False, "",
                    attach_keyboard=True,
                )
            assert "keyboard" in FakeHttpxClient._requests[-2][2]["json"]
            assert "keyboard" not in FakeHttpxClient._requests[-1][2]["json"]
            assert FakeHttpxClient._requests[-1][2]["json"]["content"] == "帮助"

        _run(body())

    def test_send_file_uses_official_file_fields(self) -> None:
        async def body():
            client = _make_client()
            client._access_token = "AT"
            client._token_expires_at = 9e18
            FakeHttpxClient._requests = []
            FakeHttpxClient._responses = [
                FakeHttpxResponse(json_data={"file_info": "FI"}),
                FakeHttpxResponse(json_data={"id": "file-msg"}),
            ]
            with patch(
                "app.adapters.qq_websocket.httpx.AsyncClient", FakeHttpxClient
            ):
                assert await client.send_file(
                    "u-1", b"data", "文档.pdf", msg_id="m-1"
                ) is True
            upload = FakeHttpxClient._requests[0][2]["json"]
            send = FakeHttpxClient._requests[1][2]["json"]
            assert upload["file_type"] == 4
            assert upload["file_name"] == "文档.pdf"
            assert send["msg_type"] == 7
            assert send["media"]["file_info"] == "FI"

        _run(body())

