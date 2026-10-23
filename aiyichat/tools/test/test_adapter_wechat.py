"""微信 ClawBot 适配器测试（伪造 aiohttp + mock 引擎/服务）。

覆盖 WeChatClient 的解析/去重/AES/MIME/登录/收发消息/多媒体分发，
以及 WeChatBotManager 的生命周期管理。
"""

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import mongomock_motor

from app.adapters.wechat_clawbot import (
    SessionExpiredError,
    WeChatBotManager,
    WeChatClient,
    get_wechat_manager,
)
from app.db import mongo
from tools.test.fake_http import FakeAiohttpResponse, FakeAiohttpSession


def _run(coro) -> None:
    asyncio.run(coro)


def _make_client(**kwargs) -> WeChatClient:
    kwargs.setdefault("bot_token", "tok")
    client = WeChatClient(bot_id="bot-1234abcd", **kwargs)
    client.engine = SimpleNamespace()
    return client


class TestStaticHelpers:
    """静态/纯函数：AES、MIME、请求头。"""

    def test_aes_roundtrip_16byte(self) -> None:
        key = b"0123456789abcdef"
        enc = WeChatClient._aes_encrypt(b"hello world", key)
        dec = WeChatClient._aes_decrypt(enc, key)
        assert dec == b"hello world"

    def test_parse_aes_key_variants(self) -> None:
        # 16 字节直接使用
        k16 = base64.b64encode(b"0123456789abcdef").decode()
        assert WeChatClient._parse_aes_key(k16) == b"0123456789abcdef"
        # 32 字节 hex ASCII
        hex_ascii = ("ab" * 16).encode("ascii")
        k32 = base64.b64encode(hex_ascii).decode()
        assert WeChatClient._parse_aes_key(k32) == bytes.fromhex("ab" * 16)
        # 非法 hex 兜底截取前 16 字节
        bad = base64.b64encode(b"zz" * 16).decode()
        assert len(WeChatClient._parse_aes_key(bad)) == 16

    def test_aes_decrypt_bad_padding(self) -> None:
        key = b"0123456789abcdef"
        # 构造一个填充字节非法的块
        raw = WeChatClient._aes_encrypt(b"x" * 16, key)
        # 直接解密另一个随机块，padding 可能越界 → 返回原文
        out = WeChatClient._aes_decrypt(raw, key)
        assert isinstance(out, bytes)

    def test_detect_image_mime(self) -> None:
        assert WeChatClient._detect_image_mime(b"\x89PNG\r\n\x1a\n") == "image/png"
        assert WeChatClient._detect_image_mime(b"\xff\xd8ffff") == "image/jpeg"
        assert (
            WeChatClient._detect_image_mime(b"RIFF" + b"0000" + b"WEBP")
            == "image/webp"
        )
        assert WeChatClient._detect_image_mime(b"GIF8xxxx") == "image/gif"
        assert WeChatClient._detect_image_mime(b"\x00\x01\x02\x03") == "image/jpeg"

    def test_get_headers(self) -> None:
        client = _make_client()
        headers = client._get_headers()
        assert headers["AuthorizationType"] == "ilink_bot_token"
        assert headers["Authorization"] == "Bearer tok"
        assert "X-WECHAT-UIN" in headers

    def test_set_bot_token_and_context(self) -> None:
        client = _make_client(bot_token="")
        client.set_bot_token("new-tok")
        assert client._bot_token == "new-tok"
        # context_token 缓存与预检
        assert client.can_send_proactive("u-1") is False
        client._context_tokens["u-1"] = "ctx"
        assert client.can_send_proactive("u-1") is True
        assert client._get_context_token("u-1") == "ctx"
        assert client._get_context_token("u-x") == ""


class TestParseAndDedup:
    """消息解析与去重。"""

    def test_parse_incoming(self) -> None:
        async def body():
            client = _make_client()
            raw = {
                "message_id": 1,
                "from_user_id": "u-1",
                "from_nickname": "小明",
                "item_list": [{"type": 1, "text_item": {"text": "你好"}}],
            }
            msg = await client.parse_incoming(raw)
            assert msg is not None and msg.content == "你好"
            assert msg.platform == "wechat" and msg.user_id == "u-1"
            # 无文本返回 None
            assert await client.parse_incoming({"item_list": []}) is None

        _run(body())

    def test_dedup(self) -> None:
        async def body():
            client = _make_client()
            client._dedup_max_size = 3
            assert await client._is_msg_duplicate(1) is False
            assert await client._is_msg_duplicate(1) is True
            for i in range(2, 8):
                await client._is_msg_duplicate(i)
            # 超过上限后最早的被淘汰
            assert len(client._processed_msg_ids) <= 3

        _run(body())


class TestHttpFlows:
    """基于伪造 aiohttp session 的 HTTP 流程。"""

    def test_get_login_qrcode_success(self) -> None:
        async def body():
            client = _make_client()
            payload = json.dumps({
                "ret": 0, "qrcode": "QR123",
                "qrcode_img_content": "https://example/qr",
            })
            client._session = FakeAiohttpSession([
                FakeAiohttpResponse(read=payload.encode("utf-8"))
            ])
            result = await client.get_login_qrcode()
            assert result["qrcode"] == "QR123"
            # 生成了二维码图片 data URI（qrcode 库已安装）
            assert result["qrcode_url"].startswith("data:image/png;base64,")

        _run(body())

    def test_get_login_qrcode_error(self) -> None:
        async def body():
            client = _make_client()
            client._session = FakeAiohttpSession([
                FakeAiohttpResponse(
                    read=json.dumps({"ret": -1, "msg": "boom"}).encode()
                )
            ])
            try:
                await client.get_login_qrcode()
                raise AssertionError("应抛出 RuntimeError")
            except RuntimeError as e:
                assert "boom" in str(e)

        _run(body())

    def test_poll_qrcode_status(self) -> None:
        async def body():
            client = _make_client()
            # 成功拿到 bot_token
            client._session = FakeAiohttpSession([
                FakeAiohttpResponse(text=json.dumps({
                    "ret": 0, "bot_token": "BT", "ilink_user_id": "uid-1",
                }))
            ])
            assert await client.poll_qrcode_status("QR") == "BT"
            # 超时错误码 → None
            client._session = FakeAiohttpSession([
                FakeAiohttpResponse(text=json.dumps({"ret": -14}))
            ])
            assert await client.poll_qrcode_status("QR") is None
            # 无 bot_token → None
            client._session = FakeAiohttpSession([
                FakeAiohttpResponse(text=json.dumps({"ret": 0}))
            ])
            assert await client.poll_qrcode_status("QR") is None

        _run(body())

    def test_complete_login_with_callback(self) -> None:
        async def body():
            cb = AsyncMock()
            client = _make_client(on_token_update=cb)
            client._session = FakeAiohttpSession([
                FakeAiohttpResponse(text=json.dumps({"typing_ticket": "TT"}))
            ])
            result = await client.complete_login("BT2")
            assert result["bot_token"] == "BT2"
            assert result["typing_ticket"] == "TT"
            cb.assert_awaited()

        _run(body())

    def test_send_text(self) -> None:
        async def body():
            client = _make_client()
            client._context_tokens["u-1"] = "ctx"
            session = FakeAiohttpSession([
                FakeAiohttpResponse(text=json.dumps({"ret": 0}))
            ])
            client._session = session
            assert await client.send_text("u-1", "hi") is True
            assert session.calls  # 发出请求
            # send_message 包装成功返回 True
            assert await client.send_message("u-1", "hi") is True

            # 平台业务错误必须返回 False，并把原因留给调度器
            client._session = FakeAiohttpSession([
                FakeAiohttpResponse(
                    text=json.dumps({"ret": 100, "msg": "context invalid"})
                )
            ])
            assert await client.send_text("u-1", "hi") is False
            assert client.last_send_error == "context invalid"
            # 空文本同样视为失败
            assert await client.send_text("u-1", "   ") is False

        _run(body())

    def test_stop_closes_session(self) -> None:
        async def body():
            client = _make_client()
            session = FakeAiohttpSession()
            client._session = session
            await client.stop()
            assert session.closed is True and client._running is False

        _run(body())

    def test_polling_loop_session_expired(self) -> None:
        async def body():
            client = _make_client()
            client._session = FakeAiohttpSession([
                FakeAiohttpResponse(text=json.dumps({"ret": -14}))
            ])
            try:
                await client._polling_loop()
                raise AssertionError("应抛 SessionExpiredError")
            except SessionExpiredError:
                pass

        _run(body())

    def test_polling_loop_dispatches_messages(self) -> None:
        async def body():
            client = _make_client()
            client._handle_message = AsyncMock()
            data = {
                "ret": 0,
                "get_updates_buf": "cursor-2",
                "msgs": [{"message_id": 9, "item_list": []}],
            }
            client._session = FakeAiohttpSession([
                FakeAiohttpResponse(text=json.dumps(data))
            ])
            await client._polling_loop()
            client._handle_message.assert_awaited()
            assert client._get_updates_buf == "cursor-2"

        _run(body())


class TestHandleMessage:
    """收消息主流程（mock 引擎与发送方法）。"""

    def test_text_message_flow(self) -> None:
        async def body():
            client = _make_client()
            client.engine.handle_message = AsyncMock(return_value=SimpleNamespace(
                text="你好呀", texts=[], image_tasks=[], conversation_id="c-1",
                voice_id="v", voice_instruction="", voice_pitch_rate=1.0,
                voice_emotion_tag="", account_id="acc-1",
            ))
            client.send_text = AsyncMock()
            client._send_typing = AsyncMock()

            msg = {
                "message_id": 100,
                "from_user_id": "u-1",
                "from_nickname": "小明",
                "context_token": "ctx-1",
                "item_list": [{"type": 1, "text_item": {"text": "在吗"}}],
            }
            with patch(
                "app.adapters.wechat_clawbot.spawn", lambda coro: coro.close()
            ):
                await client._handle_message(msg)
            client.engine.handle_message.assert_awaited()
            client.send_text.assert_awaited()
            # context_token 已缓存
            assert client._context_tokens["u-1"] == "ctx-1"

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
            client.send_text = AsyncMock()
            client._send_typing = AsyncMock()

            msg = {
                "message_id": 101,
                "from_user_id": "u-1",
                "from_nickname": "小明",
                "context_token": "ctx-1",
                "item_list": [{"type": 1, "text_item": {"text": "聊聊吧"}}],
            }
            with patch.object(settings, "multi_message_enabled", True), patch(
                "app.adapters.wechat_clawbot.asyncio.sleep", new=AsyncMock()
            ) as mock_sleep, patch(
                "app.adapters.wechat_clawbot.spawn", lambda coro: coro.close()
            ):
                await client._handle_message(msg)

            # 逐条发送 3 次，接收人与内容正确
            assert client.send_text.await_count == 3
            sent_identities = [c.args[0] for c in client.send_text.await_args_list]
            sent_texts = [c.args[1] for c in client.send_text.await_args_list]
            assert sent_identities == ["u-1"] * 3
            assert sent_texts == ["第一条", "第二条", "第三条"]
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
            client.send_text = AsyncMock()
            client._send_typing = AsyncMock()

            msg = {
                "message_id": 102,
                "from_user_id": "u-1",
                "from_nickname": "小明",
                "context_token": "ctx-1",
                "item_list": [{"type": 1, "text_item": {"text": "在吗"}}],
            }
            with patch.object(settings, "multi_message_enabled", False), patch(
                "app.adapters.wechat_clawbot.spawn", lambda coro: coro.close()
            ):
                await client._handle_message(msg)

            assert client.send_text.await_count == 1
            assert client.send_text.await_args.args[1] == "完整回复"

        _run(body())

    def test_empty_text_short_circuits(self) -> None:
        async def body():
            client = _make_client()
            client.engine.handle_message = AsyncMock()
            with patch(
                "app.adapters.wechat_clawbot.spawn", lambda coro: coro.close()
            ):
                await client._handle_message({
                    "message_id": 101, "from_user_id": "u-1", "item_list": [],
                })
            # 无文本，不应调用引擎
            client.engine.handle_message.assert_not_called()

        _run(body())

    def test_engine_exception_fallback(self) -> None:
        async def body():
            client = _make_client()
            client.engine.handle_message = AsyncMock(side_effect=RuntimeError("x"))
            client.send_text = AsyncMock()
            client._send_typing = AsyncMock()
            with patch(
                "app.adapters.wechat_clawbot.spawn", lambda coro: coro.close()
            ):
                await client._handle_message({
                    "message_id": 102, "from_user_id": "u-1",
                    "item_list": [{"type": 1, "text_item": {"text": "hi"}}],
                })
            # 异常兜底仍发送安慰文案
            client.send_text.assert_awaited()

        _run(body())


class TestAesMediaAndProactive:
    """CDN 下载解密、图片保存、主动消息。"""

    def test_download_cdn_media_missing_fields(self) -> None:
        async def body():
            client = _make_client()
            assert await client._download_cdn_media({}) is None

        _run(body())

    def test_send_proactive(self) -> None:
        async def body():
            client = _make_client()
            client.send_text = AsyncMock(return_value=True)
            # 无 context_token → 失败
            assert await client.send_proactive("u-1", "hi") is False
            # 有 token → 成功
            client._context_tokens["u-1"] = "ctx"
            assert await client.send_proactive("u-1", "hi") is True
            client.send_text.assert_awaited()
            # 平台发送失败必须原样返回 False（不能吞成成功）
            client.send_text = AsyncMock(return_value=False)
            assert await client.send_proactive("u-1", "hi") is False

        _run(body())

    def test_persist_and_load_context_tokens(self) -> None:
        async def body():
            mongo._db = mongomock_motor.AsyncMongoMockClient()["test_wx_ctx"]
            try:
                client = _make_client()
                await client._persist_context_token("u-1", "ctx-1")
                # 新客户端从 DB 恢复
                client2 = _make_client()
                await client2.load_context_tokens()
                assert client2._context_tokens.get("u-1") == "ctx-1"
            finally:
                mongo._db = None

        _run(body())


class TestBotManager:
    """WeChatBotManager 生命周期。"""

    def test_create_and_registry_ops(self) -> None:
        async def body():
            mongo._db = mongomock_motor.AsyncMongoMockClient()["test_wx_mgr"]
            try:
                mgr = WeChatBotManager(poll_interval=1)
                client = await mgr.create_bot(name="测试Bot", account_id="acc-1")
                cfg_id = client.bot_id
                # 处于 pending，get_client 可取到
                assert mgr.get_client(cfg_id) is client
                assert mgr.is_bot_running(cfg_id) is False

                # 启动任务（真实 client.start 因无网络会抛错，被 supervised_run 吞掉）
                client._bot_token = ""  # 无 token，start 立即返回
                mgr.start_bot_task(cfg_id, client)
                assert mgr.get_client(cfg_id) is client
                await asyncio.sleep(0.05)
                assert any(
                    b["config_id"] == cfg_id for b in mgr.list_bots()
                )

                # 取消任务
                await mgr.cancel_bot_task(cfg_id)
                assert mgr.get_client(cfg_id) is None

                # remove_bot（已取消再删）
                await mgr.remove_bot(cfg_id)
            finally:
                mongo._db = None

        _run(body())

    def test_apply_diff_add_remove(self) -> None:
        async def body():
            mongo._db = mongomock_motor.AsyncMongoMockClient()["test_wx_diff"]
            try:
                from app.models.bot_config import BotConfigInDB

                mgr = WeChatBotManager(poll_interval=1)
                # patch _start_bot / _stop_bot 记录调用，避免真实网络
                started: list[str] = []
                stopped: list[str] = []

                async def fake_start(cfg):
                    started.append(cfg.id)

                async def fake_stop(cid):
                    stopped.append(cid)

                mgr._start_bot = fake_start  # type: ignore[assignment]
                mgr._stop_bot = fake_stop  # type: ignore[assignment]

                online = BotConfigInDB(
                    id="b-online", name="在线", platform="wechat",
                    status="online", bot_token="tok",
                )
                await mgr._apply_diff([online])
                assert started == ["b-online"]

                # 模拟已注册后离线 → 触发 stop
                mgr._registry["b-online"] = (online, object(), object())
                await mgr._apply_diff([])
                assert stopped == ["b-online"]
            finally:
                mongo._db = None

        _run(body())

    def test_get_wechat_manager_singleton(self) -> None:
        m1 = get_wechat_manager()
        m2 = get_wechat_manager()
        assert m1 is m2
