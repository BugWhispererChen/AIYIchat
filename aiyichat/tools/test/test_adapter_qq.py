"""QQ WebSocket 适配器测试：qq_websocket。

使用 monkeypatch 替换 httpx.AsyncClient，避免真实网络；
用 mock 引擎覆盖消息事件处理与命令分发。
"""

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


from app.adapters.qq_websocket import (
    BotManager,
    QQWebSocketClient,
    _decode_qq_face,
    _QQ_FACE_RE,
)
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


def _make_client() -> QQWebSocketClient:
    client = QQWebSocketClient(_make_bot_config())
    client.engine = SimpleNamespace()
    return client


class FakeHttpxResponse:
    """伪造 httpx 响应。"""

    def __init__(self, *, json_data=None, status_code=200, text="") -> None:
        self._json = json_data if json_data is not None else {}
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._json


class FakeHttpxClient:
    """伪造 httpx.AsyncClient 异步上下文管理器。"""

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def post(self, *args, **kwargs):
        return FakeHttpxClient._response

    async def get(self, *args, **kwargs):
        return FakeHttpxClient._response

    _response = FakeHttpxResponse()


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
        resp = FakeHttpxResponse(json_data={
            "code": 40034122, "message": "召回消息已达区间上限",
        })
        client._check_rate_limit_error(resp, "u-1")
        assert "u-1" in client._rate_limited_openids
        # 非限流码不记录
        client._check_rate_limit_error(
            FakeHttpxResponse(json_data={"code": 0}), "u-2"
        )
        assert "u-2" not in client._rate_limited_openids

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
            client._rate_limited_openids["u-1"] = (
                datetime.now(timezone.utc) + timedelta(hours=1)
            )
            assert await client.send_proactive("u-1", "hi") is False

        _run(body())

    def test_send_proactive_ok(self) -> None:
        async def body():
            client = _make_client()
            client._send_reply = AsyncMock(return_value=True)
            assert await client.send_proactive("u-1", "hi") is True
            client._send_reply.assert_awaited()

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
            await client._handle_message_event("C2C_MESSAGE_CREATE", data)
            client.engine.handle_message.assert_awaited()
            client._send_reply.assert_awaited()

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

    def test_dispatch_ready_and_resumed(self) -> None:
        async def body():
            client = _make_client()
            await client._handle_dispatch({
                "t": "READY", "d": {"session_id": "s-1", "user": {"username": "bot"}},
            })
            await client._handle_dispatch({"t": "RESUMED", "d": {}})

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

