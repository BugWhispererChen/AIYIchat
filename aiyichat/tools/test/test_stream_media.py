"""流式媒体下发：语音/图片/表情包事件、落库与额度退款。

这里直接测引擎的 ``_stream_media_events``，不经过 LLM/SSE：
- 语音回复按整轮一条扣 voice 额度，TTS 失败退还；
- 图片按张扣 image 额度，生成失败退还；
- 表情包不扣额度，媒体不存在时跳过；
- 事件顺序固定为 语音 → 图片 → 表情包。
"""

import asyncio
from types import SimpleNamespace
from typing import Any

from app.config import settings
from app.conversation.engine import ConversationEngine
from app.conversation.sticker_task import StickerTask


class _FakeQuota:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.deducted: list[tuple[str, str, int]] = []
        self.refunded: list[tuple[str, str, int]] = []

    async def deduct(self, account_id: str, kind: str, amount: int):
        self.deducted.append((account_id, kind, amount))
        return self.ok, "" if self.ok else "额度不足"

    async def refund(self, account_id: str, kind: str, amount: int) -> None:
        self.refunded.append((account_id, kind, amount))


class _FakeTTS:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def synthesize(self, text: str, **kwargs: Any) -> bytes:
        self.calls.append((text, kwargs))
        if self.fail:
            raise RuntimeError("tts boom")
        return b"ID3" + b"\x00" * 16


class _FakeImageGen:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.tasks: list[Any] = []

    async def generate_for_task(self, task: Any) -> bytes:
        self.tasks.append(task)
        if self.fail:
            raise RuntimeError("image boom")
        return b"image-bytes"


class _FakeMessageRepo:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        return object()


class _FakeMediaRepo:
    async def find_by_id(self, media_id: str):
        if media_id == "stk-1":
            return {"data": b"gif"}
        return None


def _profile() -> SimpleNamespace:
    return SimpleNamespace(
        voice_id="voice-1",
        reference_image_url="",
        persona=SimpleNamespace(appearance=""),
        sticker_group_ids=[],
        language_style=SimpleNamespace(forbidden_words=[]),
        verbosity="standard",
    )


def _engine(monkeypatch, *, quota: _FakeQuota, tts: _FakeTTS,
            imagegen: _FakeImageGen, messages: _FakeMessageRepo,
            sticker_tasks: list[StickerTask] | None = None) -> ConversationEngine:
    engine = ConversationEngine(bot_id="bot-1")
    engine.quota_repo = quota  # type: ignore[assignment]

    monkeypatch.setattr(settings, "tts_enabled", True)
    monkeypatch.setattr(settings, "tts_expression_enabled", False)
    monkeypatch.setattr(settings, "imagegen_enabled", True)
    monkeypatch.setattr(settings, "sticker_enabled", True)
    monkeypatch.setattr(settings, "consistency_check_enabled", False)

    monkeypatch.setattr("app.services.tts_service.TTSService", lambda: tts)
    monkeypatch.setattr(
        "app.services.imagegen_service.ImageGenService", lambda: imagegen
    )
    monkeypatch.setattr(
        "app.db.repositories.message_repo.MessageRepository", lambda: messages
    )
    monkeypatch.setattr(
        "app.db.repositories.media_repo.MediaRepository", lambda: _FakeMediaRepo()
    )

    async def _save_audio(data: bytes, content_type: str = "audio/mpeg") -> str:
        _save_audio.calls.append((data, content_type))  # type: ignore[attr-defined]
        return "/api/media/audio-1"

    _save_audio.calls = []  # type: ignore[attr-defined]
    monkeypatch.setattr(
        "app.services.media_storage.save_audio_from_bytes", _save_audio
    )

    async def _save_image(data: bytes) -> str:
        _save_image.calls.append(data)  # type: ignore[attr-defined]
        return "/api/media/image-1"

    _save_image.calls = []  # type: ignore[attr-defined]
    monkeypatch.setattr(
        "app.services.media_storage.save_image_from_bytes", _save_image
    )

    async def _sticker_tasks(*args: Any, **kwargs: Any) -> list[StickerTask]:
        return list(sticker_tasks or [])

    engine._resolve_sticker_tasks = _sticker_tasks  # type: ignore[method-assign]
    return engine


def _run(engine: ConversationEngine, *, voice_reply: bool = True,
         image_specs: list[tuple[str, bool]] | None = None):
    async def go():
        return [
            event async for event in engine._stream_media_events(
                clean_text="你好呀",
                user_text="你好",
                image_specs=image_specs if image_specs is not None else [("一只猫", False)],
                sticker_specs=[],
                profile=_profile(),
                mood=None,
                service=SimpleNamespace(account_id="acct-1"),
                conversation=SimpleNamespace(id="conv-1"),
                emotion_tag=None,
                voice_reply=voice_reply,
            )
        ]

    return asyncio.run(go())


def test_media_events_in_order_and_messages_saved(monkeypatch) -> None:
    quota = _FakeQuota()
    tts = _FakeTTS()
    imagegen = _FakeImageGen()
    messages = _FakeMessageRepo()
    engine = _engine(
        monkeypatch, quota=quota, tts=tts, imagegen=imagegen, messages=messages,
        sticker_tasks=[StickerTask(description="笑", media_url="/api/media/stk-1")],
    )

    events = _run(engine)

    assert [e.media_kind for e in events] == ["audio", "image", "sticker"]
    assert [e.media_url for e in events] == [
        "/api/media/audio-1", "/api/media/image-1", "/api/media/stk-1",
    ]
    assert quota.deducted == [("acct-1", "voice", 1), ("acct-1", "image", 1)]
    assert quota.refunded == []
    assert tts.calls[0][0] == "你好呀"
    assert [c["content_type"] for c in messages.calls] == [
        "voice", "image", "image",
    ]
    assert [c["media_url"] for c in messages.calls] == [
        "/api/media/audio-1", "/api/media/image-1", "/api/media/stk-1",
    ]


def test_tts_failure_refunds_voice(monkeypatch) -> None:
    quota = _FakeQuota()
    engine = _engine(
        monkeypatch, quota=quota, tts=_FakeTTS(fail=True),
        imagegen=_FakeImageGen(), messages=_FakeMessageRepo(),
    )

    events = _run(engine, image_specs=[])

    assert events == []
    assert quota.deducted == [("acct-1", "voice", 1)]
    assert quota.refunded == [("acct-1", "voice", 1)]


def test_image_failure_refunds_that_image_and_keeps_sticker(monkeypatch) -> None:
    quota = _FakeQuota()
    engine = _engine(
        monkeypatch, quota=quota, tts=_FakeTTS(),
        imagegen=_FakeImageGen(fail=True), messages=_FakeMessageRepo(),
        sticker_tasks=[StickerTask(description="笑", media_url="/api/media/stk-1")],
    )

    events = _run(engine, voice_reply=False)

    assert [e.media_kind for e in events] == ["sticker"]
    assert quota.deducted == [("acct-1", "image", 1)]
    assert quota.refunded == [("acct-1", "image", 1)]


def test_quota_exhausted_emits_no_paid_media(monkeypatch) -> None:
    quota = _FakeQuota(ok=False)
    tts = _FakeTTS()
    imagegen = _FakeImageGen()
    engine = _engine(
        monkeypatch, quota=quota, tts=tts, imagegen=imagegen,
        messages=_FakeMessageRepo(),
    )

    events = _run(engine)

    assert events == []
    assert quota.deducted == [("acct-1", "voice", 1), ("acct-1", "image", 1)]
    assert quota.refunded == []
    assert tts.calls == [] and imagegen.tasks == []


def test_text_turn_does_not_call_tts(monkeypatch) -> None:
    quota = _FakeQuota()
    tts = _FakeTTS()
    engine = _engine(
        monkeypatch, quota=quota, tts=tts,
        imagegen=_FakeImageGen(), messages=_FakeMessageRepo(),
    )

    events = _run(engine, voice_reply=False, image_specs=[])

    assert events == []
    assert tts.calls == []
    assert quota.deducted == []


def test_stream_text_extracts_media_specs(monkeypatch) -> None:
    monkeypatch.setattr(settings, "consistency_check_enabled", False)
    engine = ConversationEngine(bot_id="bot-1")
    clean, images, stickers = engine._resolve_stream_text(
        "你好[IMG:一只猫][表情:笑]",
        None,
        _profile(),
        "identity-1",
    )
    assert clean == "你好"
    assert images and images[0][0] == "一只猫"
    assert stickers == ["笑"]
