"""媒体文件存储服务：下载、压缩、存入 MongoDB。"""

import asyncio
import io
import logging
import uuid

import httpx

from app.db.repositories.media_repo import (
    MediaQuotaExceeded,
    MediaRepository,
)
from app.utils.redaction import mask_secret

logger = logging.getLogger(__name__)

__all__ = ["MediaQuotaExceeded"]

MAX_IMAGE_SIZE = 4 * 1024 * 1024  # 落库图片上限 4 MB
MAX_INBOUND_IMAGE_DOWNLOAD = 10 * 1024 * 1024  # 入站图片原始下载上限 10 MB
MAX_AUDIO_SIZE = 10 * 1024 * 1024  # 音频文件上限 10 MB
MAX_FILE_SIZE = 15 * 1024 * 1024  # 文件/视频上限 15 MB（低于 Mongo 16MB 文档硬限）

# 只允许光栅图片；SVG/HTML 等可执行/可渲染文本不能作为图片落库
_ALLOWED_IMAGE_FORMATS = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "GIF": "image/gif",
    "WEBP": "image/webp",
    "BMP": "image/bmp",
}

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


def normalize_inbound_image(data: bytes) -> tuple[bytes, str]:
    """校验入站图片是受支持的光栅格式，并规范化 MIME。"""
    if not data:
        raise ValueError("图片为空")

    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as image:
            image_format = (image.format or "").upper()
            if image_format not in _ALLOWED_IMAGE_FORMATS:
                raise ValueError(f"不支持的图片格式: {image_format or 'unknown'}")
            # verify 会完整检查文件结构，避免把截断/伪装文件当图片存下去
            image.verify()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("图片格式不受支持") from exc

    content_type = _ALLOWED_IMAGE_FORMATS[image_format]
    if len(data) > MAX_IMAGE_SIZE:
        data, content_type = _compress_image(data, content_type)
        if len(data) > MAX_IMAGE_SIZE:
            raise ValueError("图片压缩后仍超过大小上限")
    return data, content_type


async def download_inbound_image(
    url: str, max_bytes: int = MAX_INBOUND_IMAGE_DOWNLOAD
) -> tuple[bytes, str]:
    """流式下载入站图片，边读边检查上限，避免把超大响应当作 bytes 全量读入。"""
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("图片地址必须是 http(s)")

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            content_type = (
                resp.headers.get("content-type", "").split(";")[0].strip()
            )
            declared = resp.headers.get("content-length")
            if declared:
                try:
                    declared_size = int(declared)
                except ValueError:
                    declared_size = 0
                if declared_size > max_bytes:
                    raise ValueError("图片超过大小上限")

            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("图片超过大小上限")
                chunks.append(chunk)
    return b"".join(chunks), content_type


async def _save_media(
    data: bytes,
    content_type: str,
    *,
    filename: str = "",
    owner_account_id: str = "",
) -> str:
    """配额检查通过后写入媒体库；配额不足会抛出 MediaQuotaExceeded。"""
    await _media_repo.ensure_capacity(owner_account_id, len(data))
    return await _media_repo.create(
        data=data,
        content_type=content_type,
        filename=filename,
        owner_account_id=owner_account_id,
    )


async def save_inbound_image_from_url(
    url: str, owner_account_id: str = ""
) -> tuple[str, bytes]:
    """下载入站图片、校验格式、压缩并落库，返回 (media_url, 原始字节)。"""
    raw_data, _declared_type = await download_inbound_image(url)
    data, content_type = await asyncio.to_thread(
        normalize_inbound_image, raw_data
    )
    doc_id = await _save_media(
        data, content_type, owner_account_id=owner_account_id
    )
    # public_id 是 URL 里的能力令牌（见 MediaRepository.create），日志只打掩码值
    logger.info("Image saved to DB: id=%s, size=%d", mask_secret(doc_id), len(data))
    return f"/api/media/{doc_id}", raw_data


async def save_image_from_bytes(
    data: bytes,
    content_type: str = "image/png",
    owner_account_id: str = "",
) -> str:
    """二进制图片 → 压缩 → 存入 MongoDB → 返回 /api/media/{id}。"""
    data, content_type = await asyncio.to_thread(
        _compress_image, data, content_type
    )
    doc_id = await _save_media(
        data, content_type, owner_account_id=owner_account_id
    )
    logger.info("Image saved to DB: id=%s, size=%d", mask_secret(doc_id), len(data))
    return f"/api/media/{doc_id}"


QQ_UPLOAD_MAX = 1 * 1024 * 1024  # QQ 文件上传接口限制约 1MB

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


async def save_file_from_bytes(
    data: bytes,
    filename: str = "file.bin",
    content_type: str = "application/octet-stream",
    owner_account_id: str = "",
) -> str:
    """二进制文件/视频原样存入媒体库，返回可鉴权的 /api/media/{id}。"""
    if len(data) > MAX_FILE_SIZE:
        raise ValueError(
            f"文件过大 ({len(data) / 1024 / 1024:.1f} MB)，"
            f"上限 {MAX_FILE_SIZE / 1024 / 1024:.0f} MB"
        )
    doc_id = await _save_media(
        data,
        content_type,
        filename=filename,
        owner_account_id=owner_account_id,
    )
    logger.info(
        "File saved to DB: id=%s, size=%d, type=%s",
        mask_secret(doc_id), len(data), content_type,
    )
    return f"/api/media/{doc_id}"


async def save_audio_from_bytes(
    data: bytes,
    content_type: str = "audio/mpeg",
    owner_account_id: str = "",
) -> str:
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
    doc_id = await _save_media(
        data,
        content_type,
        filename=filename,
        owner_account_id=owner_account_id,
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
