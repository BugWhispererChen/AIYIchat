"""音频格式转换工具：silk ↔ wav，QQ媒体下载。"""

import asyncio
import logging
import struct
import tempfile
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


async def download_qq_media(url: str, timeout: float = 30.0) -> bytes:
    """异步下载QQ富媒体文件。"""
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


async def silk_to_wav(silk_data: bytes) -> bytes:
    """silk 音频 → 24kHz mono WAV。"""

    def _convert(data: bytes) -> bytes:
        import pilk

        with tempfile.NamedTemporaryFile(suffix=".silk", delete=False) as sf:
            sf.write(data)
            silk_path = sf.name
        pcm_path = silk_path.replace(".silk", ".pcm")
        try:
            duration = pilk.decode(silk_path, pcm_path)
            logger.debug("silk decoded: %.1fs", duration / 1000)
            pcm_data = Path(pcm_path).read_bytes()
            return _pcm_to_wav(pcm_data, sample_rate=24000, channels=1, sample_width=2)
        finally:
            Path(silk_path).unlink(missing_ok=True)
            Path(pcm_path).unlink(missing_ok=True)

    return await asyncio.to_thread(_convert, silk_data)


async def wav_to_silk(wav_data: bytes) -> bytes:
    """24kHz mono WAV → silk 音频（QQ发送格式）。"""

    def _convert(data: bytes) -> bytes:
        import pilk

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as wf:
            wf.write(data)
            wav_path = wf.name
        pcm_path = wav_path.replace(".wav", ".pcm")
        silk_path = wav_path.replace(".wav", ".silk")
        try:
            # wav → pcm via ffmpeg
            import subprocess

            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", wav_path,
                    "-f", "s16le", "-ar", "24000", "-ac", "1",
                    pcm_path,
                ],
                capture_output=True,
                check=True,
            )
            duration = pilk.encode(pcm_path, silk_path, pcm_rate=24000, tencent=True)
            logger.debug("silk encoded: %.1fs", duration / 1000)
            return Path(silk_path).read_bytes()
        finally:
            Path(wav_path).unlink(missing_ok=True)
            Path(pcm_path).unlink(missing_ok=True)
            Path(silk_path).unlink(missing_ok=True)

    return await asyncio.to_thread(_convert, wav_data)


def _pcm_to_wav(
    pcm_data: bytes,
    sample_rate: int = 24000,
    channels: int = 1,
    sample_width: int = 2,
) -> bytes:
    """原始 PCM 数据包装为 WAV 格式。"""
    data_size = len(pcm_data)
    byte_rate = sample_rate * channels * sample_width
    block_align = channels * sample_width
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM
        channels,
        sample_rate,
        byte_rate,
        block_align,
        sample_width * 8,
        b"data",
        data_size,
    )
    return header + pcm_data


async def mp3_to_silk(mp3_data: bytes) -> tuple[bytes, int]:
    """MP3 音频 -> silk 音频（微信发送格式）。

    流程: mp3 -> ffmpeg 转 24kHz mono PCM -> pilk.encode -> silk

    Returns:
        (silk_bytes, duration_ms) 元组
    """

    def _convert(data: bytes) -> tuple[bytes, int]:
        import subprocess

        import pilk

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as mf:
            mf.write(data)
            mp3_path = mf.name
        pcm_path = mp3_path.replace(".mp3", ".pcm")
        silk_path = mp3_path.replace(".mp3", ".silk")
        try:
            # mp3 -> 24kHz mono s16le PCM
            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", mp3_path,
                    "-f", "s16le", "-ar", "24000", "-ac", "1",
                    pcm_path,
                ],
                capture_output=True,
                check=True,
            )
            duration = pilk.encode(pcm_path, silk_path, pcm_rate=24000, tencent=True)
            logger.debug("mp3->silk encoded: %.1fs", duration / 1000)
            silk_data = Path(silk_path).read_bytes()
            return silk_data, duration
        finally:
            Path(mp3_path).unlink(missing_ok=True)
            Path(pcm_path).unlink(missing_ok=True)
            Path(silk_path).unlink(missing_ok=True)

    return await asyncio.to_thread(_convert, mp3_data)
