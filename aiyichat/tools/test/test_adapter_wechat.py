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
            mongo._db = mongomock_motor.AsyncMongoMockClient()["test_wx_dedup"]
            try:
                client = _make_client()
                client._dedup_max_size = 3
                assert await client._is_msg_duplicate("1") is False
                await client._mark_msg_processed("1")
                assert await client._is_msg_duplicate("1") is True
                for i in range(2, 8):
                    await client._mark_msg_processed(str(i))
                # 超过上限后最早的被淘汰
                assert len(client._processed_msg_ids) <= 3
            finally:
                mongo._db = None

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
            # 确认状态返回凭据供服务层保存，但不直接暴露给路由。
            client._session = FakeAiohttpSession([
                FakeAiohttpResponse(text=json.dumps({
                    "ret": 0, "status": "confirmed",
                    "bot_token": "BT", "ilink_user_id": "uid-1",
                }))
            ])
            result = await client.poll_qrcode_status("QR")
            assert result["status"] == "confirmed"
            assert result["bot_token"] == "BT"
            # 超时错误码与空状态均保持 wait。
            client._session = FakeAiohttpSession([
                FakeAiohttpResponse(text=json.dumps({"ret": -14}))
            ])
            assert await client.poll_qrcode_status("QR") == {"status": "wait"}
            client._session = FakeAiohttpSession([
                FakeAiohttpResponse(text=json.dumps({"ret": 0}))
            ])
            assert await client.poll_qrcode_status("QR") == {"status": "wait"}

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
            mongo._db = mongomock_motor.AsyncMongoMockClient()["test_wx_poll"]
            try:
                client = _make_client()
                client._handle_message = AsyncMock()
                data = {
                    "ret": 0,
                    "get_updates_buf": "cursor-2",
                    "msgs": [{"message_id": 9, "item_list": []}],
                }
                session = FakeAiohttpSession([
                    FakeAiohttpResponse(text=json.dumps(data))
                ])
                client._session = session
                await client._polling_loop()
                client._handle_message.assert_awaited()
                assert client._get_updates_buf == "cursor-2"
                payload = session.calls[0][2]["json"]
                assert set(payload) == {"get_updates_buf", "base_info"}
                assert payload["get_updates_buf"] == ""
                assert set(payload["base_info"]) == {"channel_version", "bot_agent"}
            finally:
                mongo._db = None

        _run(body())


class TestIlinkReliability:
    """iLink 持久游标、去重与完整媒体 URL 契约。"""

    def test_cursor_restart_and_duplicate_repull(self) -> None:
        async def body():
            mongo._db = mongomock_motor.AsyncMongoMockClient()["test_wx_restart"]
            try:
                data = {
                    "ret": 0,
                    "get_updates_buf": "cursor-2",
                    "msgs": [{"message_id": 9001, "item_list": []}],
                }
                first = _make_client()
                first._handle_message = AsyncMock()
                first._session = FakeAiohttpSession([
                    FakeAiohttpResponse(text=json.dumps(data))
                ])
                await first._polling_loop()
                assert first._get_updates_buf == "cursor-2"

                # 新进程客户端恢复游标和回执，重拉同一消息不再进入处理器。
                second = _make_client()
                await second.load_sync_state()
                assert second._get_updates_buf == "cursor-2"
                second._handle_message = AsyncMock()
                second._session = FakeAiohttpSession([
                    FakeAiohttpResponse(text=json.dumps({
                        **data, "get_updates_buf": "cursor-3"
                    }))
                ])
                await second._polling_loop()
                second._handle_message.assert_not_awaited()
                assert second._get_updates_buf == "cursor-3"

                third = _make_client()
                await third.load_sync_state()
                assert third._get_updates_buf == "cursor-3"
            finally:
                mongo._db = None

        _run(body())

    def test_processing_failure_does_not_advance_cursor(self) -> None:
        async def body():
            mongo._db = mongomock_motor.AsyncMongoMockClient()["test_wx_failure"]
            try:
                client = _make_client()
                await client._persist_get_updates_buf("cursor-old")
                client._handle_message = AsyncMock(side_effect=RuntimeError("boom"))
                client._session = FakeAiohttpSession([
                    FakeAiohttpResponse(text=json.dumps({
                        "ret": 0,
                        "get_updates_buf": "cursor-new",
                        "msgs": [{"message_id": 1, "item_list": []}],
                    }))
                ])
                await client._polling_loop()
                assert client._get_updates_buf == "cursor-old"
                doc = await mongo.get_database().wechat_sync_state.find_one(
                    {"bot_id": client.bot_id}
                )
                assert doc["get_updates_buf"] == "cursor-old"
                assert doc.get("processed_msg_ids", []) == []
            finally:
                mongo._db = None

        _run(body())

    def test_cdn_download_full_url_and_encoded_fallback(self) -> None:
        async def body():
            key = base64.b64encode(b"0123456789abcdef").decode()
            client = _make_client()
            with patch.object(WeChatClient, "_aes_decrypt", return_value=b"plain"):
                session = FakeAiohttpSession([FakeAiohttpResponse(read=b"cipher")])
                client._session = session
                item = {"media": {
                    "full_url": "https://cdn.example.test/download?id=1",
                    "encrypt_query_param": "ignored",
                    "aes_key": key,
                }}
                assert await client._download_cdn_media(item) == b"plain"
                assert session.calls[0][1] == item["media"]["full_url"]

                session = FakeAiohttpSession([FakeAiohttpResponse(read=b"cipher")])
                client._session = session
                item = {"media": {
                    "encrypt_query_param": "a+b/c=",
                    "aes_key": key,
                }}
                assert await client._download_cdn_media(item) == b"plain"
                assert session.calls[0][1].endswith(
                    "encrypted_query_param=a%2Bb%2Fc%3D"
                )

        _run(body())

    def test_cdn_upload_full_url_and_encoded_fallback(self) -> None:
        async def body():
            client = _make_client()
            client._get_upload_url = AsyncMock(return_value={
                "ret": 0,
                "upload_full_url": "https://cdn.example.test/upload?ticket=1",
                "upload_param": "ignored",
            })
            session = FakeAiohttpSession([
                FakeAiohttpResponse(headers={"x-encrypted-param": "download"})
            ])
            client._session = session
            assert await client._upload_cdn_media(b"image", "u-1", "image")
            assert session.calls[0][1] == "https://cdn.example.test/upload?ticket=1"

            client._get_upload_url = AsyncMock(return_value={
                "ret": 0, "upload_param": "a+b/c="
            })
            session = FakeAiohttpSession([
                FakeAiohttpResponse(headers={"x-encrypted-param": "download"})
            ])
            client._session = session
            assert await client._upload_cdn_media(b"image", "u-1", "image")
            assert "encrypted_query_param=a%2Bb%2Fc%3D" in session.calls[0][1]

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


class TestP0QrLifecycleAndFileContract:
    """P0：扫码状态机、验证码/重定向、生命周期取消与文件字段。"""

    def test_qr_verify_code_and_redirect_state_machine(self) -> None:
        async def body():
            client = _make_client()
            client._session = FakeAiohttpSession([
                FakeAiohttpResponse(text=json.dumps({
                    "ret": 0, "status": "need_verifycode",
                })),
                FakeAiohttpResponse(text=json.dumps({
                    "ret": 0, "status": "scaned_but_redirect",
                    "redirect_host": "redirect.example.test",
                })),
                FakeAiohttpResponse(text=json.dumps({
                    "ret": 0, "status": "verify_code_blocked",
                })),
            ])
            first = await client.poll_qrcode_status("QR", "123456")
            assert first["status"] == "need_verifycode"
            assert client._session.calls[0][2]["params"]["verify_code"] == "123456"
            second = await client.poll_qrcode_status("QR", "123456")
            assert second["status"] == "scaned_but_redirect"
            third = await client.poll_qrcode_status("QR")
            assert third["status"] == "verify_code_blocked"
            assert client._session.calls[2][1].startswith(
                "https://redirect.example.test/"
            )

        _run(body())

    def test_confirmed_baseurl_is_persisted_and_restored(self) -> None:
        async def body():
            old_db = mongo._db
            mongo._db = mongomock_motor.AsyncMongoMockClient()["test_wx_baseurl"]
            try:
                client = _make_client()
                client._session = FakeAiohttpSession([
                    FakeAiohttpResponse(text=json.dumps({
                        "ret": 0,
                        "status": "confirmed",
                        "bot_token": "BT",
                        "baseurl": "https://api2.example.test",
                    }))
                ])
                result = await client.poll_qrcode_status("QR")
                assert result["status"] == "confirmed"
                assert client._bot_url("/ilink/bot/getupdates").startswith(
                    "https://api2.example.test/"
                )
                restored = _make_client(bot_token="BT")
                await restored.load_sync_state()
                assert restored._api_base_url == "https://api2.example.test"
            finally:
                mongo._db = old_db

        _run(body())

    def test_stop_cancels_inflight_poll_and_notifies_platform(self) -> None:
        async def body():
            client = _make_client()
            client._running = True
            session = FakeAiohttpSession([
                FakeAiohttpResponse(text=json.dumps({"ret": 0}))
            ])
            client._session = session

            async def never_finishes():
                await asyncio.sleep(60)

            client._poll_task = asyncio.create_task(never_finishes())
            await client.stop()
            assert client._poll_task is None
            assert any(
                url.endswith("/ilink/bot/msg/notifystop")
                for _method, url, _kwargs in session.calls
            )

        _run(body())

    def test_notify_start_uses_base_info(self) -> None:
        async def body():
            client = _make_client()
            client._session = FakeAiohttpSession([
                FakeAiohttpResponse(text=json.dumps({"ret": 0}))
            ])
            await client._notify_lifecycle("start")
            method, url, kwargs = client._session.calls[0]
            assert method == "POST"
            assert url.endswith("/ilink/bot/msg/notifystart")
            assert set(kwargs["json"]["base_info"]) == {
                "channel_version", "bot_agent",
            }

        _run(body())

    def test_send_file_uses_len_and_checks_business_result(self) -> None:
        async def body():
            client = _make_client()
            client._context_tokens["u-1"] = "ctx"
            client._upload_cdn_media = AsyncMock(return_value={
                "cdn_media": {
                    "encrypt_query_param": "p",
                    "aes_key": "a",
                    "encrypt_type": 1,
                },
                "raw_size": 123,
                "cipher_size": 128,
            })
            client._session = FakeAiohttpSession([
                FakeAiohttpResponse(text=json.dumps({"ret": 0}))
            ])
            assert await client.send_file("u-1", b"data", "file.pdf") is True
            payload = client._session.calls[0][2]["json"]
            file_item = payload["msg"]["item_list"][0]["file_item"]
            assert file_item["len"] == "123"
            assert "file_size" not in file_item

            client._session = FakeAiohttpSession([
                FakeAiohttpResponse(text=json.dumps({
                    "ret": 100, "msg": "context invalid",
                }))
            ])
            assert await client.send_file("u-1", b"data", "file.pdf") is False
            assert client.last_send_error == "context invalid"

        _run(body())


class TestP1QuoteAndFiles:
    """P1：持久引用缓存与入站文件。"""

    def test_quote_cache_is_restart_safe_and_isolated(self) -> None:
        async def body():
            from app.db.repositories.wechat_quote_repo import (
                WeChatQuoteCacheRepository,
            )

            old_db = mongo._db
            mongo._db = mongomock_motor.AsyncMongoMockClient()["test_wx_quote"]
            try:
                repo = WeChatQuoteCacheRepository()
                await repo.upsert(
                    bot_id="bot-a", account_id="acc-a",
                    session_key="session-1", svr_id="svr-1",
                    message_id="101", text="被引用的原文",
                    media_kind="image", media_url="/api/media/m1",
                )
                # 新实例模拟服务重启
                restored = await WeChatQuoteCacheRepository().find(
                    bot_id="bot-a", session_key="session-1", svr_id="svr-1",
                )
                assert restored and restored["text"] == "被引用的原文"
                assert await repo.find(
                    bot_id="bot-b", session_key="session-1", svr_id="svr-1",
                ) is None
                assert await repo.find(
                    bot_id="bot-a", session_key="session-2", svr_id="svr-1",
                ) is None
            finally:
                mongo._db = old_db

        _run(body())

    def test_outbound_text_and_media_are_cached_by_server_message_id(self) -> None:
        async def body():
            from app.db.repositories.wechat_quote_repo import (
                WeChatQuoteCacheRepository,
            )

            old_db = mongo._db
            mongo._db = mongomock_motor.AsyncMongoMockClient()["test_wx_out_quote"]
            try:
                client = _make_client(account_id="acc-1")
                client._context_tokens["u-1"] = "ctx"
                client._session = FakeAiohttpSession([
                    FakeAiohttpResponse(text=json.dumps({
                        "ret": 0, "message_id": "srv-out-1",
                    }))
                ])
                with patch(
                    "app.adapters.wechat_clawbot.spawn",
                    lambda coro: coro.close(),
                ):
                    assert await client.send_text("u-1", "Bot 发出的原文") is True
                cached = await WeChatQuoteCacheRepository().find(
                    bot_id=client.bot_id, session_key="u-1",
                    svr_id="srv-out-1",
                )
                assert cached and cached["role"] == "assistant"
                assert cached["text"] == "Bot 发出的原文"

                client._upload_cdn_media = AsyncMock(return_value={
                    "cdn_media": {
                        "encrypt_query_param": "p",
                        "aes_key": "a",
                        "encrypt_type": 1,
                    },
                    "raw_size": 4,
                    "cipher_size": 16,
                })
                client._session = FakeAiohttpSession([
                    FakeAiohttpResponse(text=json.dumps({
                        "ret": 0, "message_id": "srv-out-file",
                    }))
                ])
                with patch(
                    "app.adapters.wechat_clawbot.spawn",
                    lambda coro: coro.close(),
                ):
                    assert await client.send_file(
                        "u-1", b"data", "说明.txt"
                    ) is True
                cached_file = await WeChatQuoteCacheRepository().find(
                    bot_id=client.bot_id, session_key="u-1",
                    svr_id="srv-out-file",
                )
                assert cached_file and cached_file["media_kind"] == "file"
                assert "说明.txt" in cached_file["text"]
            finally:
                mongo._db = old_db

        _run(body())

    def test_quote_context_prefers_cached_text_and_media(self) -> None:
        async def body():
            from app.db.repositories.wechat_quote_repo import (
                WeChatQuoteCacheRepository,
            )

            old_db = mongo._db
            mongo._db = mongomock_motor.AsyncMongoMockClient()["test_wx_quote_ctx"]
            try:
                await WeChatQuoteCacheRepository().upsert(
                    bot_id="bot-1234abcd", account_id="acc-1",
                    session_key="u-1", svr_id="svr-9",
                    text="原消息正文", media_kind="image",
                    media_url="/api/media/m9",
                )
                client = _make_client(account_id="acc-1")
                context = await client._resolve_quote_context(
                    {"type": 1, "ref_msg": {"svr_id": "svr-9"}},
                    {"from_user_id": "u-1", "message_id": 200},
                )
                assert "原消息正文" in context
                assert "image" in context
            finally:
                mongo._db = old_db

        _run(body())

    def test_quote_uses_nested_message_item_msg_id(self) -> None:
        async def body():
            from app.db.repositories.wechat_quote_repo import (
                WeChatQuoteCacheRepository,
            )

            old_db = mongo._db
            mongo._db = mongomock_motor.AsyncMongoMockClient()["test_wx_nested_ref"]
            try:
                await WeChatQuoteCacheRepository().upsert(
                    bot_id="bot-1234abcd", account_id="acc-1",
                    session_key="u-1", svr_id="server-msg-1",
                    text="Bot 被引用的回复",
                )
                client = _make_client(account_id="acc-1")
                context = await client._resolve_quote_context({
                    "type": 1,
                    "ref_msg": {
                        "message_item": {
                            "type": 0,
                            "msg_id": "server-msg-1",
                        },
                    },
                }, {"from_user_id": "u-1", "message_id": 700})
                assert "【引用消息】Bot 被引用的回复" in context
            finally:
                mongo._db = old_db

        _run(body())

    def test_quote_message_item_accepts_string_type(self) -> None:
        async def body():
            client = _make_client(account_id="acc-1")
            context = await client._resolve_quote_context({
                "type": 1,
                "ref_msg": {
                    "svr_id": "not-cached",
                    "message_item": {
                        "type": "1",
                        "text_item": {"text": "字符串类型的引用正文"},
                    },
                },
            }, {"from_user_id": "u-1", "message_id": 600})
            assert "字符串类型的引用正文" in context

            media_context = await client._resolve_quote_context({
                "type": 1,
                "ref_msg": {
                    "svr_id": "not-cached",
                    "message_item": {
                        "type": "2",
                        "image_item": {},
                    },
                },
            }, {"from_user_id": "u-1", "message_id": 601})
            assert "image" in media_context

        _run(body())

    def test_partial_quote_uses_md5_and_never_falls_back_to_full_text(self) -> None:
        async def body():
            import hashlib

            from app.db.repositories.wechat_quote_repo import (
                WeChatQuoteCacheRepository,
            )

            old_db = mongo._db
            mongo._db = mongomock_motor.AsyncMongoMockClient()["test_wx_partial"]
            try:
                full_text = "今天天气不错我们出去走走吧"
                selected = "天气不错我们出去走走"
                await WeChatQuoteCacheRepository().upsert(
                    bot_id="bot-1234abcd", account_id="acc-1",
                    session_key="u-1", svr_id="svr-partial",
                    text=full_text,
                )
                client = _make_client(account_id="acc-1")
                msg = {"from_user_id": "u-1", "message_id": 500}
                partial = {
                    "start": "天气", "end": "走走",
                    "startindex": 0, "endindex": 0,
                    "quotemd5": hashlib.md5(selected.encode()).hexdigest(),
                }
                context = await client._resolve_quote_context({
                    "type": 1,
                    "ref_msg": {
                        "svr_id": "svr-partial",
                        "partial_text": partial,
                    },
                }, msg)
                assert f"【引用消息】{selected}" in context
                assert "今天" not in context

                bad_partial = {**partial, "quotemd5": "deadbeef"}
                mismatch = await client._resolve_quote_context({
                    "type": 1,
                    "ref_msg": {
                        "svr_id": "svr-partial",
                        "partial_text": bad_partial,
                    },
                }, msg)
                assert mismatch == "【引用消息：局部引用校验失败】"
                assert "天气" not in mismatch
            finally:
                mongo._db = old_db

        _run(body())

    def test_bot_echo_only_caches_and_never_reenters_engine(self) -> None:
        async def body():
            old_db = mongo._db
            mongo._db = mongomock_motor.AsyncMongoMockClient()["test_wx_echo"]
            try:
                client = _make_client(account_id="acc-1")
                client.engine.handle_message = AsyncMock()
                client._cache_message_without_reply = AsyncMock()
                msg = {
                    "message_id": 300,
                    "message_type": 2,
                    "to_user_id": "u-1",
                    "item_list": [{"type": 1, "text_item": {"text": "Bot 回显"}}],
                }
                await client._handle_message(msg)
                client.engine.handle_message.assert_not_awaited()
                client._cache_message_without_reply.assert_awaited_once()
            finally:
                mongo._db = old_db

        _run(body())

    def test_quote_ref_is_resolved_in_full_message_flow(self) -> None:
        async def body():
            from app.db.repositories.wechat_quote_repo import (
                WeChatQuoteCacheRepository,
            )

            old_db = mongo._db
            mongo._db = mongomock_motor.AsyncMongoMockClient()["test_wx_ref_flow"]
            try:
                await WeChatQuoteCacheRepository().upsert(
                    bot_id="bot-1234abcd", account_id="acc-1",
                    session_key="u-1", svr_id="svr-quote-1",
                    text="被引用的原句", media_kind="image",
                )
                client = _make_client(account_id="acc-1")
                client.engine.handle_message = AsyncMock(return_value=SimpleNamespace(
                    text="收到", texts=[], image_tasks=[], conversation_id="c-1",
                    voice_id="v", voice_instruction="", voice_pitch_rate=1.0,
                    voice_emotion_tag="", account_id="acc-1",
                ))
                client.send_text = AsyncMock()
                client._send_typing = AsyncMock()
                with patch(
                    "app.adapters.wechat_clawbot.spawn", lambda coro: coro.close()
                ):
                    await client._handle_message({
                        "message_id": 401,
                        "from_user_id": "u-1",
                        "item_list": [{
                            "type": 1,
                            "text_item": {"text": "这条怎么样"},
                            "ref_msg": {"svr_id": "svr-quote-1"},
                        }],
                    })
                text = client.engine.handle_message.await_args.kwargs["text"]
                assert "【引用消息】被引用的原句" in text
                assert "image" in text
                assert text.endswith("这条怎么样")
            finally:
                mongo._db = old_db

        _run(body())

    def test_incoming_video_is_saved(self) -> None:
        async def body():
            client = _make_client(account_id="acc-1")
            client._download_cdn_media = AsyncMock(return_value=b"video")
            client.engine.handle_message = AsyncMock(return_value=SimpleNamespace(
                text="收到", texts=[], image_tasks=[], conversation_id="c-1",
                voice_id="v", voice_instruction="", voice_pitch_rate=1.0,
                voice_emotion_tag="", account_id="acc-1",
            ))
            client.send_text = AsyncMock()
            client._send_typing = AsyncMock()
            client._cache_quote_message = AsyncMock()
            with patch(
                "app.services.media_storage.save_file_from_bytes",
                AsyncMock(return_value="/api/media/video-1"),
            ), patch(
                "app.adapters.wechat_clawbot.spawn", lambda coro: coro.close()
            ):
                await client._handle_message({
                    "message_id": 402,
                    "from_user_id": "u-1",
                    "item_list": [{
                        "type": 5,
                        "video_item": {"media": {
                            "encrypt_query_param": "p", "aes_key": "key",
                        }},
                    }],
                })
            kwargs = client.engine.handle_message.await_args.kwargs
            assert kwargs["content_type"] == "video"
            assert kwargs["media_url"] == "/api/media/video-1"

        _run(body())

    def test_incoming_file_is_saved_and_text_extracted(self) -> None:
        async def body():
            client = _make_client(account_id="acc-1")
            client._download_cdn_media = AsyncMock(return_value="报告正文".encode())
            client.engine.handle_message = AsyncMock(return_value=SimpleNamespace(
                text="收到", texts=[], image_tasks=[], conversation_id="c-1",
                voice_id="v", voice_instruction="", voice_pitch_rate=1.0,
                voice_emotion_tag="", account_id="acc-1",
            ))
            client.send_text = AsyncMock()
            client._send_typing = AsyncMock()
            client._cache_quote_message = AsyncMock()
            msg = {
                "message_id": 301,
                "from_user_id": "u-1",
                "item_list": [{
                    "type": 4,
                    "file_item": {
                        "file_name": "报告.txt",
                        "media": {
                            "encrypt_query_param": "p", "aes_key": "key",
                        },
                    },
                }],
            }
            with patch(
                "app.services.media_storage.save_file_from_bytes",
                AsyncMock(return_value="/api/media/file-1"),
            ), patch(
                "app.adapters.wechat_clawbot.spawn", lambda coro: coro.close()
            ):
                await client._handle_message(msg)
            kwargs = client.engine.handle_message.await_args.kwargs
            assert "报告正文" in kwargs["text"]
            assert kwargs["content_type"] == "file"
            assert kwargs["media_url"] == "/api/media/file-1"

        _run(body())


class TestP2VideoAndCacheLifecycle:
    """P2：视频发送与引用缓存清理。"""

    def test_send_video_official_payload(self) -> None:
        async def body():
            client = _make_client()
            client._context_tokens["u-1"] = "ctx"
            client._upload_cdn_media = AsyncMock(return_value={
                "cdn_media": {
                    "encrypt_query_param": "p", "aes_key": "a", "encrypt_type": 1,
                },
                "cipher_size": 42,
            })
            client._session = FakeAiohttpSession([
                FakeAiohttpResponse(text=json.dumps({"ret": 0}))
            ])
            assert await client.send_video("u-1", b"video") is True
            payload = client._session.calls[0][2]["json"]
            item = payload["msg"]["item_list"][0]
            assert item["type"] == 5
            assert item["video_item"]["video_size"] == 42

        _run(body())

    def test_remove_bot_clears_quote_cache(self) -> None:
        async def body():
            from app.db.repositories.wechat_quote_repo import (
                WeChatQuoteCacheRepository,
            )

            old_db = mongo._db
            mongo._db = mongomock_motor.AsyncMongoMockClient()["test_wx_remove"]
            try:
                await WeChatQuoteCacheRepository().upsert(
                    bot_id="bot-remove", session_key="u-1", svr_id="s-1",
                    account_id="acc-1", text="hi",
                )
                mgr = WeChatBotManager(poll_interval=1)
                await mgr.remove_bot("bot-remove")
                assert await WeChatQuoteCacheRepository().find(
                    bot_id="bot-remove", session_key="u-1", svr_id="s-1",
                ) is None
            finally:
                mongo._db = old_db

        _run(body())


class TestFileTextExtraction:
    """入站文件文本提取的格式覆盖与恶意 XML 防护。"""

    def test_text_and_json_and_unknown(self) -> None:
        from app.services.file_text import extract_text_from_file

        assert extract_text_from_file("你好".encode(), "a.txt") == "你好"
        assert "name" in extract_text_from_file(
            b'{"name": "x"}', "a.json"
        )
        assert extract_text_from_file(b"\x00\x01", "a.pdf") == ""

    def test_docx_extracts_paragraphs_and_rejects_entities(self) -> None:
        import io
        import zipfile

        from app.services.file_text import extract_text_from_file

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(
                "word/document.xml",
                '<w:document xmlns:w="http://schemas.openxmlformats.org/'
                'wordprocessingml/2006/main"><w:body><w:p><w:r>'
                '<w:t>段落正文</w:t></w:r></w:p></w:body></w:document>',
            )
        assert "段落正文" in extract_text_from_file(
            buffer.getvalue(), "a.docx"
        )

        malicious = io.BytesIO()
        with zipfile.ZipFile(malicious, "w") as archive:
            archive.writestr(
                "word/document.xml",
                '<!DOCTYPE x [<!ENTITY a "boom">]><x>&a;</x>',
            )
        assert extract_text_from_file(malicious.getvalue(), "a.docx") == ""
