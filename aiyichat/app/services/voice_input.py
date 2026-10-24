"""网页语音输入的上传约束与转码。

浏览器 MediaRecorder 产出的是 webm/ogg/mp4，而 ASR 只接受 WAV；媒体库回放又需要
体积更小的 MP3。这里统一做一次"原始字节 -> WAV(16k) + MP3(24k)"，临时文件只落在
系统临时目录且用固定文件名，绝不使用上传方提供的文件名拼路径。
"""

import asyncio
import logging
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_VOICE_BYTES = 10 * 1024 * 1024
MAX_VOICE_SECONDS = 60.0
MAX_TRANSCRIPT_CHARS = 4000
# 单账号 10 分钟窗口内的语音上传次数；ASR 是外部计费调用，必须先于额度预检限流
VOICE_RATE_LIMIT = 20

ALLOWED_AUDIO_TYPES = frozenset({
    "audio/webm",
    "audio/ogg",
    "audio/mp4",
    "audio/mpeg",
    "audio/wav",
    "audio/x-wav",
    "audio/aac",
    "audio/x-m4a",
})


class VoiceInputError(Exception):
    """可安全转成 4xx/503 的语音输入错误。"""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def normalize_audio_type(content_type: str) -> str:
    """去掉 ``;codecs=opus`` 之类的参数，只保留 MIME 主类型。"""
    return (content_type or "").split(";", 1)[0].strip().lower()


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _probe_duration(src: Path) -> float:
    try:
        result = _run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(src),
            ],
            timeout=15,
        )
    except FileNotFoundError as exc:
        raise VoiceInputError(
            "audio_tool_missing", "服务端音频处理组件不可用", 503
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise VoiceInputError("invalid_audio", "音频读取超时，请重试", 400) from exc

    if result.returncode != 0:
        raise VoiceInputError("invalid_audio", "音频无法识别，请重新录制", 400)
    raw = result.stdout.decode("utf-8", errors="ignore").strip()
    try:
        duration = float(raw)
    except ValueError as exc:
        raise VoiceInputError("invalid_audio", "音频时长无法识别", 400) from exc
    if duration <= 0:
        raise VoiceInputError("invalid_audio", "音频为空，请重新录制", 400)
    if duration > MAX_VOICE_SECONDS:
        raise VoiceInputError(
            "voice_too_long",
            f"语音最长 {int(MAX_VOICE_SECONDS)} 秒，请分段发送",
            413,
        )
    return duration


def _convert(src: Path, wav_path: Path, mp3_path: Path) -> None:
    try:
        result = _run(
            [
                "ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-i", str(src),
                "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav_path),
                "-ac", "1", "-ar", "24000", "-b:a", "64k", str(mp3_path),
            ],
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise VoiceInputError(
            "audio_tool_missing", "服务端音频处理组件不可用", 503
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise VoiceInputError("audio_convert_failed", "音频处理超时，请重试", 400) from exc

    if result.returncode != 0 or not wav_path.exists() or not mp3_path.exists():
        logger.warning(
            "语音转码失败: rc=%s stderr=%s",
            result.returncode,
            result.stderr.decode("utf-8", errors="ignore")[:300],
        )
        raise VoiceInputError("audio_convert_failed", "音频处理失败，请重新录制", 400)


def _prepare_sync(data: bytes) -> tuple[bytes, bytes, float]:
    with tempfile.TemporaryDirectory(prefix="webvoice_") as tmp:
        root = Path(tmp)
        src = root / "input.bin"
        wav_path = root / "asr.wav"
        mp3_path = root / "store.mp3"
        src.write_bytes(data)
        duration = _probe_duration(src)
        _convert(src, wav_path, mp3_path)
        return wav_path.read_bytes(), mp3_path.read_bytes(), duration


async def prepare_audio(data: bytes) -> tuple[bytes, bytes, float]:
    """返回 (ASR 用 WAV, 回放用 MP3, 时长秒)。"""
    return await asyncio.to_thread(_prepare_sync, data)


async def transcribe_wav(wav_data: bytes) -> str:
    """ASR 识别；失败返回空串（与 ASRService 的约定一致）。"""
    from app.services.asr_service import ASRService

    text = await ASRService().transcribe(wav_data, fmt="wav")
    return (text or "").strip()
