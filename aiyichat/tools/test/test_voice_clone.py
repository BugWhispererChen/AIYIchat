"""声音复刻服务与 Profile 模型扩展测试。

覆盖：
- VoiceCloneService：create_voice / list_voices / delete_voice（mock HTTP，base64 直传）
- VoiceCloneService._build_data_uri：MIME 归一化与 base64 编码
- VoiceCloneService._post：HTTP 错误与业务错误处理
- ProfileService.build_clone_prefix：前缀生成规则
- Profile 模型：voice_clone 字段默认值
- media_storage.save_audio_from_bytes：音频保存与大小校验
"""

import asyncio
import base64
from unittest.mock import AsyncMock, patch

import pytest

from app.models.profile import PromptProfileCreate, PromptProfileUpdate
from app.profiles.service import ProfileService
from app.services.voice_clone_service import VoiceCloneError, VoiceCloneService


def _run(coro):
    return asyncio.run(coro)


# ── VoiceCloneService ─────────────────────────────────────


class TestVoiceCloneService:
    """声音复刻服务：base64 直传 API 调用与错误处理。"""

    def _make_service(self) -> VoiceCloneService:
        return VoiceCloneService()

    @patch("app.services.voice_clone_service.VoiceCloneService._resolve_tts_config")
    @patch("app.services.voice_clone_service.VoiceCloneService._post")
    def test_create_voice_success(self, mock_post, mock_cfg) -> None:
        mock_cfg.return_value.api_key = "sk-test"
        mock_post.return_value = {
            "output": {"voice_id": "qwen-audio-3.0-tts-plus-myvoice-abc1234"}
        }
        svc = self._make_service()
        voice_id = _run(svc.create_voice(
            b"fake-audio-bytes",
            "audio/mpeg",
            "qwen-audio-3.0-tts-plus",
            "myvoice",
        ))
        assert voice_id == "qwen-audio-3.0-tts-plus-myvoice-abc1234"
        mock_post.assert_called_once()
        payload = mock_post.call_args[0][0]
        assert payload["model"] == "voice-enrollment"
        assert payload["input"]["action"] == "create_voice"
        assert payload["input"]["target_model"] == "qwen-audio-3.0-tts-plus"
        assert payload["input"]["prefix"] == "myvoice"
        # url 字段为 base64 Data URI 直传，不含任何外部 http(s) URL
        data = payload["input"]["url"]
        assert data.startswith("data:audio/mpeg;base64,")
        assert "http" not in data
        decoded = base64.b64decode(data.split(",", 1)[1])
        assert decoded == b"fake-audio-bytes"

    @patch("app.services.voice_clone_service.VoiceCloneService._resolve_tts_config")
    @patch("app.services.voice_clone_service.VoiceCloneService._post")
    def test_create_voice_no_voice_id(self, mock_post, mock_cfg) -> None:
        mock_cfg.return_value.api_key = "sk-test"
        mock_post.return_value = {"output": {}}
        svc = self._make_service()
        with pytest.raises(VoiceCloneError, match="未返回 voice_id"):
            _run(svc.create_voice(
                b"fake-audio-bytes",
                "audio/wav",
                "qwen-audio-3.0-tts-plus",
                "myvoice",
            ))

    @patch("app.services.voice_clone_service.VoiceCloneService._resolve_tts_config")
    @patch("app.services.voice_clone_service.VoiceCloneService._post")
    def test_create_voice_unsupported_mime(self, mock_post, mock_cfg) -> None:
        mock_cfg.return_value.api_key = "sk-test"
        svc = self._make_service()
        with pytest.raises(VoiceCloneError, match="不支持的音频类型"):
            _run(svc.create_voice(
                b"fake-audio-bytes",
                "video/mp4",
                "qwen-audio-3.0-tts-plus",
                "myvoice",
            ))

    @patch("app.services.voice_clone_service.VoiceCloneService._resolve_tts_config")
    @patch("app.services.voice_clone_service.VoiceCloneService._post")
    def test_delete_voice(self, mock_post, mock_cfg) -> None:
        mock_cfg.return_value.api_key = "sk-test"
        mock_post.return_value = {}
        svc = self._make_service()
        result = _run(svc.delete_voice("qwen-audio-3.0-tts-plus-myvoice-abc1234"))
        assert result is True
        payload = mock_post.call_args[0][0]
        assert payload["model"] == "voice-enrollment"
        assert payload["input"]["action"] == "delete_voice"
        assert payload["input"]["voice_id"] == (
            "qwen-audio-3.0-tts-plus-myvoice-abc1234"
        )

    @patch("app.services.voice_clone_service.VoiceCloneService._resolve_tts_config")
    @patch("app.services.voice_clone_service.VoiceCloneService._post")
    def test_list_voices(self, mock_post, mock_cfg) -> None:
        mock_cfg.return_value.api_key = "sk-test"
        mock_post.return_value = {
            "output": {"voice_list": [{"voice_id": "v1"}, {"voice_id": "v2"}]}
        }
        svc = self._make_service()
        voices = _run(svc.list_voices())
        assert len(voices) == 2
        payload = mock_post.call_args[0][0]
        assert payload["model"] == "voice-enrollment"
        assert payload["input"]["action"] == "list_voice"


class TestBuildDataUri:
    """_build_data_uri：MIME 归一化与 base64 编码。"""

    def test_wav_alias_normalized(self) -> None:
        uri = VoiceCloneService._build_data_uri(b"abc", "audio/x-wav")
        assert uri.startswith("data:audio/wav;base64,")

    def test_mp3_alias_normalized(self) -> None:
        uri = VoiceCloneService._build_data_uri(b"abc", "audio/mp3")
        assert uri.startswith("data:audio/mpeg;base64,")

    def test_m4a_alias_normalized(self) -> None:
        uri = VoiceCloneService._build_data_uri(b"abc", "audio/x-m4a")
        assert uri.startswith("data:audio/mp4;base64,")

    def test_case_insensitive(self) -> None:
        uri = VoiceCloneService._build_data_uri(b"abc", "AUDIO/MPEG")
        assert uri.startswith("data:audio/mpeg;base64,")

    def test_unsupported_mime_raises(self) -> None:
        with pytest.raises(VoiceCloneError, match="不支持的音频类型"):
            VoiceCloneService._build_data_uri(b"abc", "application/pdf")


class TestVoiceClonePost:
    """_post 内部方法：HTTP 与业务错误处理。"""

    @patch("app.services.voice_clone_service.httpx.AsyncClient")
    def test_http_error(self, mock_client_cls) -> None:
        mock_resp = AsyncMock()
        mock_resp.status_code = 403
        mock_resp.text = "Forbidden"
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        with pytest.raises(VoiceCloneError, match="HTTP 403"):
            _run(VoiceCloneService._post({}, "sk-test", 10.0))

    @patch("app.services.voice_clone_service.httpx.AsyncClient")
    def test_business_error(self, mock_client_cls) -> None:
        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        # json() 是同步方法，需要用 MagicMock 而非 AsyncMock
        from unittest.mock import MagicMock
        mock_resp.json = MagicMock(return_value={
            "code": "40010001",
            "message": "音频质量不合格",
        })
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        with pytest.raises(VoiceCloneError, match="音频质量不合格"):
            _run(VoiceCloneService._post({}, "sk-test", 10.0))


# ── ProfileService.build_clone_prefix ──────────────────────


class TestBuildClonePrefix:
    """角色名 → 音色前缀生成。"""

    def test_normal_name(self) -> None:
        assert ProfileService.build_clone_prefix("林黛玉") == "myvoice"

    def test_english_name(self) -> None:
        assert ProfileService.build_clone_prefix("Alice") == "alice"

    def test_mixed_name(self) -> None:
        assert ProfileService.build_clone_prefix("Alice123") == "alice123"

    def test_long_name_truncated(self) -> None:
        result = ProfileService.build_clone_prefix("VeryLongCharacterName")
        assert len(result) <= 10

    def test_special_chars_removed(self) -> None:
        assert ProfileService.build_clone_prefix("test-name_2.0") == "testname20"

    def test_empty_name(self) -> None:
        assert ProfileService.build_clone_prefix("") == "myvoice"


# ── Profile 模型扩展字段 ────────────────────────────────────


class TestProfileVoiceCloneFields:
    """Profile 模型 voice_clone 字段的默认值。"""

    def test_create_defaults(self) -> None:
        p = PromptProfileCreate(slug="test", name="测试")
        assert p.voice_clone_status == ""
        assert p.voice_clone_target_model == ""

    def test_update_fields(self) -> None:
        u = PromptProfileUpdate(
            voice_clone_status="active",
            voice_clone_target_model="qwen-audio-3.0-tts-plus",
        )
        assert u.voice_clone_status == "active"
        assert u.voice_clone_target_model == "qwen-audio-3.0-tts-plus"

    def test_update_defaults_none(self) -> None:
        u = PromptProfileUpdate()
        assert u.voice_clone_status is None
        assert u.voice_clone_target_model is None


# ── media_storage.save_audio_from_bytes ─────────────────────


class TestSaveAudioFromBytes:
    """音频保存到媒体存储（试听场景）。"""

    @patch("app.services.media_storage._media_repo")
    def test_save_mp3(self, mock_repo) -> None:
        mock_repo.create = AsyncMock(return_value="media123")
        from app.services.media_storage import save_audio_from_bytes

        url = _run(save_audio_from_bytes(b"fake-mp3-data", "audio/mpeg"))
        assert url == "/api/media/media123"
        call_kwargs = mock_repo.create.call_args[1]
        assert call_kwargs["content_type"] == "audio/mpeg"
        assert call_kwargs["filename"].endswith(".mp3")

    @patch("app.services.media_storage._media_repo")
    def test_save_wav(self, mock_repo) -> None:
        mock_repo.create = AsyncMock(return_value="media456")
        from app.services.media_storage import save_audio_from_bytes

        url = _run(save_audio_from_bytes(b"fake-wav-data", "audio/wav"))
        assert url == "/api/media/media456"
        call_kwargs = mock_repo.create.call_args[1]
        assert call_kwargs["filename"].endswith(".wav")

    def test_oversized_audio_rejected(self) -> None:
        from app.services.media_storage import MAX_AUDIO_SIZE, save_audio_from_bytes

        big_data = b"x" * (MAX_AUDIO_SIZE + 1)
        with pytest.raises(ValueError, match="音频文件过大"):
            _run(save_audio_from_bytes(big_data, "audio/mpeg"))
