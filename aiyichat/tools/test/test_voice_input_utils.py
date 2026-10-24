"""语音输入的纯函数边界：MIME 归一化与 TTS 音频容器识别。"""

from app.services.audio_utils import audio_content_type
from app.services.voice_input import normalize_audio_type


def test_normalize_audio_type_strips_codecs_and_case() -> None:
    assert normalize_audio_type("audio/WebM;codecs=opus") == "audio/webm"
    assert normalize_audio_type(" audio/mp4 ") == "audio/mp4"
    assert normalize_audio_type("") == ""


def test_audio_content_type_detects_wav_and_mp3() -> None:
    wav = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 16
    mp3 = b"ID3" + b"\x00" * 16
    assert audio_content_type(wav) == "audio/wav"
    assert audio_content_type(mp3) == "audio/mpeg"
    assert audio_content_type(b"") == "audio/mpeg"
