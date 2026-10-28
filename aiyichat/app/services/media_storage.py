"""媒体文件存储服务：下载、压缩、存入 MongoDB。"""

import asyncio
import io
import logging
import uuid

import httpx

from app.db.repositories.media_repo import MediaRepository
from app.utils.redaction import mask_secret

logger = logging.getLogger(__name__)

MAX_IMAGE_SIZE = 4 * 1024 * 1024  # 4 MB

_media_repo = MediaRepository()


def _compress_image(
    data: bytes, content_type: str, max_size: int = MAX_IMAGE_SIZE
) -> tuple[bytes, str]:
    """
    若图片超过 max_size，用 Pillow 逐步降低质量/缩小尺寸。
    返回 (压缩后 bytes, content_type)。
    """
    if len(data) <= max_size:
        return data, content_type

    from PIL import Image

    img: Image.Image = Image.open(io.BytesIO(data))

    # 转为 RGB（去掉 alpha 通道以便保存为 JPEG）
    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")

    out_ct = "image/jpeg"

    # 第一轮：逐步降低 JPEG 质量
    for quality in (85, 70, 55, 40, 25):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        if buf.tell() <= max_size:
            return buf.getvalue(), out_ct

    # 第二轮：缩小尺寸 + 低质量
    for scale in (0.75, 0.5, 0.35, 0.25):
        new_w = int(img.width * scale)
        new_h = int(img.height * scale)
        resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        resized.save(buf, format="JPEG", quality=40, optimize=True)
        if buf.tell() <= max_size:
            return buf.getvalue(), out_ct

    # 最终兜底：取最后一次结果
    return buf.getvalue(), out_ct


async def save_image_from_url(url: str) -> str:
    """下载远程图片 → 压缩 → 存入 MongoDB → 返回 /api/media/{id}。"""
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()

    raw_data = resp.content
    ct = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()

    # 压缩（在线程中执行，避免阻塞事件循环）
    data, ct = await asyncio.to_thread(_compress_image, raw_data, ct)

    doc_id = await _media_repo.create(data=data, content_type=ct)
    # public_id 是 URL 里的能力令牌（见 MediaRepository.create），日志只打掩码值
    logger.info("Image saved to DB: id=%s, size=%d", mask_secret(doc_id), len(data))
    return f"/api/media/{doc_id}"


async def save_image_from_bytes(data: bytes, content_type: str = "image/png") -> str:
    """二进制图片 → 压缩 → 存入 MongoDB → 返回 /api/media/{id}。"""
    data, ct = await asyncio.to_thread(_compress_image, data, content_type)
    doc_id = await _media_repo.create(data=data, content_type=ct)
    logger.info("Image saved to DB: id=%s, size=%d", mask_secret(doc_id), len(data))
    return f"/api/media/{doc_id}"


QQ_UPLOAD_MAX = 1 * 1024 * 1024  # QQ 文件上传接口限制约 1MB

MAX_AUDIO_SIZE = 10 * 1024 * 1024  # 音频文件上限 10 MB

# 音频 content_type → 文件扩展名映射
_AUDIO_EXT_MAP = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/m4a": "m4a",
}


MAX_FILE_SIZE = 100 * 1024 * 1024  # 通用文件/视频上限 100 MB


async def save_file_from_bytes(
    data: bytes,
    filename: str = "file.bin",
    content_type: str = "application/octet-stream",
) -> str:
    """二进制文件/视频原样存入媒体库，返回可鉴权的 /api/media/{id}。"""
    if len(data) > MAX_FILE_SIZE:
        raise ValueError(
            f"文件过大 ({len(data) / 1024 / 1024:.1f} MB)，"
            f"上限 {MAX_FILE_SIZE / 1024 / 1024:.0f} MB"
        )
    doc_id = await _media_repo.create(
        data=data,
        content_type=content_type,
        filename=filename,
    )
    logger.info(
        "File saved to DB: id=%s, size=%d, type=%s",
        mask_secret(doc_id), len(data), content_type,
    )
    return f"/api/media/{doc_id}"


async def save_audio_from_bytes(data: bytes, content_type: str = "audio/mpeg") -> str:
    """二进制音频 → 存入 MongoDB → 返回 /api/media/{id}。

    供试听等场景内部播放使用（不对外提供给第三方服务）。音频不压缩，直接存储。
    """
    if len(data) > MAX_AUDIO_SIZE:
        raise ValueError(
            f"音频文件过大 ({len(data) / 1024 / 1024:.1f} MB)，"
            f"上限 {MAX_AUDIO_SIZE / 1024 / 1024:.0f} MB"
        )
    ext = _AUDIO_EXT_MAP.get(content_type.lower(), "mp3")
    filename = f"voice_clone_{uuid.uuid4().hex}.{ext}"
    doc_id = await _media_repo.create(
        data=data, content_type=content_type, filename=filename
    )
    logger.info(
        "Audio saved to DB: id=%s, size=%d, type=%s",
        mask_secret(doc_id), len(data), content_type,
    )
    return f"/api/media/{doc_id}"


async def compress_for_qq(data: bytes, content_type: str = "image/png") -> bytes:
    """压缩图片到 QQ 上传可接受的大小（<= 1MB）。"""
    compressed, _ = await asyncio.to_thread(
        _compress_image, data, content_type, QQ_UPLOAD_MAX
    )
    return compressed
