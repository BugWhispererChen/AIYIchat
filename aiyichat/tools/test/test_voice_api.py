"""网页语音输入端点：ASR → 文本流式回复；上限/限流/归属校验。

复用 test_api_auth 的建号与登录夹具，避免两套网页鉴权初始化漂移。
"""

import asyncio
from typing import Any, AsyncIterator

import pytest

from app.config import settings
from app.db import mongo
from app.services.captcha_service import captcha_service
from app.utils.rate_limit import account_limiter, login_limiter, register_limiter
from tools.test.test_api_auth import (
    _WEB_KEYS,
    _approve,
    _client,
    _login,
    _register,
    _service_for_username,
    _setup_app,
)


@pytest.fixture(autouse=True)
def voice_web_flags():
    saved = {k: getattr(settings, k) for k in _WEB_KEYS}
    settings.web_auth_enabled = True
    settings.web_register_enabled = True
    settings.web_register_require_review = True
    settings.web_captcha_enabled = False
    settings.web_rate_limit_register = 100
    settings.web_rate_limit_login = 100
    settings.web_session_hours = 24
    settings.web_session_max_concurrent = 3
    settings.web_login_max_fail = 5
    settings.web_free_quota_template_id = ""
    settings.trust_proxy_headers = True
    register_limiter.reset()
    login_limiter.reset()
    account_limiter.reset()
    captcha_service.reset()
    yield
    for key, value in saved.items():
        setattr(settings, key, value)
    register_limiter.reset()
    login_limiter.reset()
    account_limiter.reset()
    captcha_service.reset()


def _patch_voice(monkeypatch, *, transcript: str = "你好"):
    from app.api import conversations_api

    async def _prepare(data: bytes):
        _prepare.calls.append(data)  # type: ignore[attr-defined]
        return b"wav-bytes", b"mp3-bytes", 1.0

    _prepare.calls = []  # type: ignore[attr-defined]

    async def _transcribe(wav: bytes) -> str:
        return transcript

    async def _save(data: bytes, content_type: str = "audio/mpeg") -> str:
        return "/api/media/user-voice"

    monkeypatch.setattr(settings, "asr_enabled", True)
    monkeypatch.setattr(conversations_api, "prepare_audio", _prepare)
    monkeypatch.setattr(conversations_api, "transcribe_wav", _transcribe)
    monkeypatch.setattr(
        "app.services.media_storage.save_audio_from_bytes", _save
    )
    return _prepare


async def _post_voice(client, token: str, *, service_id: str = ""):
    return await client.post(
        "/api/conversations/chat/voice/stream",
        files={"audio": ("voice.webm", b"raw-audio", "audio/webm")},
        data={"service_id": service_id},
        headers={"Authorization": f"Bearer {token}"},
    )


def _payloads(body: str) -> list[str]:
    return [
        line.removeprefix("data: ")
        for line in body.splitlines()
        if line.startswith("data:")
    ]


def test_voice_upload_streams_user_text_and_reply(monkeypatch) -> None:
    import app.conversation.engine as engine_module
    from app.conversation.stream_gate import StreamEvent

    seen: dict[str, Any] = {}

    class _VoiceEngine:
        def __init__(self, bot_id: str = "") -> None:
            self.bot_id = bot_id

        async def handle_message_stream(self, **kwargs: Any) -> AsyncIterator[Any]:
            seen.update(kwargs)
            yield StreamEvent(kind="msg", text="收到")
            yield StreamEvent(
                kind="media", media_kind="audio",
                media_url="/api/media/ai-voice",
            )

    _patch_voice(monkeypatch)

    async def flow() -> None:
        app = await _setup_app("t_voice_stream")
        original = engine_module.ConversationEngine
        engine_module.ConversationEngine = _VoiceEngine  # type: ignore[misc]
        try:
            async with _client(app) as client:
                await _register(client, "voiceuser")
                await _approve("voiceuser")
                token = await _login(client, "voiceuser")
                service = await _service_for_username("voiceuser")
                resp = await _post_voice(client, token, service_id=service.id)
                assert resp.status_code == 200, resp.text
                payloads = _payloads(resp.text)
                assert payloads[0] == "[USER_TEXT]你好", payloads
                assert "[MSG]" in payloads and "收到" in payloads
                assert "[MEDIA]audio|/api/media/ai-voice" in payloads
                assert payloads[-1] == "[DONE]", payloads
                assert seen["content_type"] == "voice"
                assert seen["media_url"] == "/api/media/user-voice"
                assert seen["voice_reply"] is True
                assert seen["text"] == "你好"
        finally:
            engine_module.ConversationEngine = original  # type: ignore[misc]
            mongo._db = None

    asyncio.run(flow())


def test_voice_asr_disabled_503(monkeypatch) -> None:
    monkeypatch.setattr(settings, "asr_enabled", False)

    async def flow() -> None:
        app = await _setup_app("t_voice_disabled")
        try:
            async with _client(app) as client:
                await _register(client, "voicedisabled")
                await _approve("voicedisabled")
                token = await _login(client, "voicedisabled")
                resp = await _post_voice(client, token)
                assert resp.status_code == 503, resp.text
        finally:
            mongo._db = None

    asyncio.run(flow())


def test_empty_transcript_422(monkeypatch) -> None:
    _patch_voice(monkeypatch, transcript="")

    async def flow() -> None:
        app = await _setup_app("t_voice_empty")
        try:
            async with _client(app) as client:
                await _register(client, "voiceempty")
                await _approve("voiceempty")
                token = await _login(client, "voiceempty")
                service = await _service_for_username("voiceempty")
                resp = await _post_voice(client, token, service_id=service.id)
                assert resp.status_code == 422, resp.text
        finally:
            mongo._db = None

    asyncio.run(flow())


def test_invalid_mime_415() -> None:
    async def flow() -> None:
        app = await _setup_app("t_voice_mime")
        try:
            async with _client(app) as client:
                await _register(client, "voicemime")
                await _approve("voicemime")
                token = await _login(client, "voicemime")
                service = await _service_for_username("voicemime")
                resp = await client.post(
                    "/api/conversations/chat/voice/stream",
                    files={"audio": ("voice.txt", b"not-audio", "text/plain")},
                    data={"service_id": service.id},
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert resp.status_code == 415, resp.text
        finally:
            mongo._db = None

    asyncio.run(flow())


def test_unknown_service_rejected_before_asr(monkeypatch) -> None:
    prepare = _patch_voice(monkeypatch)

    async def flow() -> None:
        app = await _setup_app("t_voice_owner")
        try:
            async with _client(app) as client:
                await _register(client, "voiceowner")
                await _approve("voiceowner")
                token = await _login(client, "voiceowner")
                resp = await _post_voice(
                    client, token, service_id="missing-service"
                )
                assert resp.status_code == 404, resp.text
                assert prepare.calls == []  # type: ignore[attr-defined]
        finally:
            mongo._db = None

    asyncio.run(flow())


def test_voice_rate_limit_429(monkeypatch) -> None:
    from app.api import conversations_api
    from app.conversation.stream_gate import StreamEvent

    class _VoiceEngine:
        def __init__(self, bot_id: str = "") -> None:
            self.bot_id = bot_id

        async def handle_message_stream(self, **kwargs: Any) -> AsyncIterator[Any]:
            yield StreamEvent(kind="msg", text="好")

    _patch_voice(monkeypatch)
    monkeypatch.setattr(conversations_api, "VOICE_RATE_LIMIT", 1)

    async def flow() -> None:
        import app.conversation.engine as engine_module

        app = await _setup_app("t_voice_rate")
        original = engine_module.ConversationEngine
        engine_module.ConversationEngine = _VoiceEngine  # type: ignore[misc]
        try:
            async with _client(app) as client:
                await _register(client, "voicerate")
                await _approve("voicerate")
                token = await _login(client, "voicerate")
                service = await _service_for_username("voicerate")
                first = await _post_voice(client, token, service_id=service.id)
                second = await _post_voice(client, token, service_id=service.id)
                assert first.status_code == 200, first.text
                assert second.status_code == 429, second.text
        finally:
            engine_module.ConversationEngine = original  # type: ignore[misc]
            account_limiter.reset()
            mongo._db = None

    asyncio.run(flow())


def test_history_messages_include_media_fields() -> None:
    from app.db.repositories.conversation_repo import ConversationRepository
    from app.db.repositories.message_repo import MessageRepository

    async def flow() -> None:
        app = await _setup_app("t_history_media")
        try:
            async with _client(app) as client:
                await _register(client, "historyuser")
                await _approve("historyuser")
                token = await _login(client, "historyuser")
                service = await _service_for_username("historyuser")
                conv = await ConversationRepository().create(
                    service.identity_id, "profile-1", service.id
                )
                await MessageRepository().create(
                    conversation_id=conv.id,
                    role="assistant",
                    content="(语音回复)",
                    content_type="voice",
                    media_url="/api/media/history-voice",
                )
                resp = await client.get(
                    f"/api/conversations/{conv.id}/messages?limit=10",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert resp.status_code == 200, resp.text
                rows = resp.json()
                assert rows[0]["content_type"] == "voice"
                assert rows[0]["media_url"] == "/api/media/history-voice"
        finally:
            mongo._db = None

    asyncio.run(flow())
