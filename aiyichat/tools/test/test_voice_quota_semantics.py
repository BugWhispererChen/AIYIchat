"""语音额度口径：按"条"扣、失败退还。

背景：后台把语音额度显示成"剩余 N 次"，适配器却按 `len(text)` 扣字符——
30 次额度会被一条长语音一次耗尽；而 TTS 合成/发送失败时降级成了文字，
用户仍然为这条没发出去的语音付了费（`QuotaRepository.refund` 全库无人调用）。
"""

import asyncio
from typing import Any

import pytest

from app.adapters.qq_websocket import QQWebSocketClient
from app.adapters.wechat_clawbot import WeChatClient


class _RecordingQuota:
    """记录扣/退调用的 QuotaRepository 替身。"""

    calls: list[tuple[str, str, int]] = []
    allow = True

    @classmethod
    def reset(cls) -> None:
        cls.calls = []
        cls.allow = True

    def __init__(self) -> None:
        pass

    async def deduct(self, account_id: str, content_type: str, amount: int) -> tuple[bool, str]:
        type(self).calls.append(("deduct", content_type, amount))
        return (True, "") if self.allow else (False, "额度不足")

    async def refund(self, account_id: str, content_type: str, amount: int) -> bool:
        type(self).calls.append(("refund", content_type, amount))
        return True


class _TTS:
    def __init__(self, *, fail: bool) -> None:
        self._fail = fail

    async def synthesize(self, *args: Any, **kwargs: Any) -> bytes:
        if self._fail:
            raise RuntimeError("合成失败")
        return b"fake-mp3"


def _wechat_client(**overrides: Any) -> WeChatClient:
    """绕过 __init__ 造一个只带被测协程所需属性的客户端。"""
    client = object.__new__(WeChatClient)
    client._label = "test-bot"
    client._tts = overrides.get("tts", _TTS(fail=False))
    client.sent_text: list[str] = []
    client.sent_files: list[str] = []

    async def send_text(to_user_id: str, text: str) -> bool:
        client.sent_text.append(text)
        return True

    async def send_file(to_user_id: str, data: bytes, name: str) -> bool:
        client.sent_files.append(name)
        return True

    client.send_text = send_text  # type: ignore[method-assign]
    client.send_file = send_file  # type: ignore[method-assign]
    return client


@pytest.fixture
def quota_repo(monkeypatch: pytest.MonkeyPatch) -> type[_RecordingQuota]:
    _RecordingQuota.reset()
    import app.db.repositories.quota_repo as quota_module

    monkeypatch.setattr(quota_module, "QuotaRepository", _RecordingQuota)
    return _RecordingQuota


def test_voice_quota_charged_once_per_message(quota_repo: type[_RecordingQuota]) -> None:
    """长文本也只扣 1 次：额度单位是"条"，不是字符。"""
    client = _wechat_client()
    asyncio.run(client._fire_and_forget_tts(
        "user-1", "这是一段很长很长的语音文案" * 20, account_id="acct-1"
    ))
    assert quota_repo.calls == [("deduct", "voice", 1)]
    assert client.sent_files == ["voice_reply.mp3"]


def test_voice_quota_refunded_when_send_fails(quota_repo: type[_RecordingQuota]) -> None:
    """合成失败：退还额度并降级为文字，用户不为发不出去的语音付费。"""
    client = _wechat_client(tts=_TTS(fail=True))
    asyncio.run(client._fire_and_forget_tts(
        "user-1", "语音文案", account_id="acct-1"
    ))
    assert quota_repo.calls == [("deduct", "voice", 1), ("refund", "voice", 1)]
    assert client.sent_text == ["语音文案"]


def test_voice_quota_not_refunded_when_not_charged(quota_repo: type[_RecordingQuota]) -> None:
    """额度不足时直接降级文字：没有扣费就没有退还，也不该凭空退回一次。"""
    quota_repo.allow = False
    client = _wechat_client(tts=_TTS(fail=True))
    asyncio.run(client._fire_and_forget_tts("user-1", "语音文案", account_id="acct-1"))
    assert quota_repo.calls == [("deduct", "voice", 1)]
    assert client.sent_text == ["语音文案"]


def test_qq_voice_quota_charged_once_per_message(
    quota_repo: type[_RecordingQuota],
) -> None:
    """QQ 侧与微信侧口径一致：按条扣，失败退还。"""
    adapter = object.__new__(QQWebSocketClient)
    adapter._label = "qq-test"
    adapter._tts = _TTS(fail=True)
    sent: list[str] = []

    async def _send_reply(user_id: str, text: str, *args: Any, **kwargs: Any) -> None:
        sent.append(text)

    adapter._send_reply = _send_reply  # type: ignore[method-assign]
    asyncio.run(adapter._fire_and_forget_tts(
        "user-1", "很长的语音文案" * 30, "msg-1", False, "", account_id="acct-1"
    ))
    assert quota_repo.calls == [("deduct", "voice", 1), ("refund", "voice", 1)]
    assert sent == ["很长的语音文案" * 30]
