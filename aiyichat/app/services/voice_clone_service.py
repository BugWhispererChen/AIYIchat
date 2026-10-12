"""声音复刻服务：调用阿里云百炼 voice-enrollment API 创建/管理自定义音色。

音频以 base64 Data URI 通过 url 字段直传阿里云（实测可用），
无需对外提供可下载的音频 URL。
"""

import base64
import logging

import httpx

from app.llm.model_registry import ResolvedModelConfig, resolve_model_config

logger = logging.getLogger(__name__)

# 阿里云声音复刻 API 端点（华北2-北京）
_VOICE_ENROLLMENT_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization"
)

# 创建音色请求超时（秒）：base64 音频体较大，适当放宽
_CREATE_TIMEOUT = 60.0
# 查询/删除音色请求超时（秒）
_QUERY_TIMEOUT = 15.0

# 上传 content_type → Data URI MIME 归一化（官方白名单：wav/mpeg/mp4）
_MIME_NORMALIZE = {
    "audio/wav": "audio/wav",
    "audio/x-wav": "audio/wav",
    "audio/wave": "audio/wav",
    "audio/mpeg": "audio/mpeg",
    "audio/mp3": "audio/mpeg",
    "audio/mp4": "audio/mp4",
    "audio/m4a": "audio/mp4",
    "audio/x-m4a": "audio/mp4",
}


class VoiceCloneError(Exception):
    """声音复刻操作失败。"""


class VoiceCloneService:
    """声音复刻服务：音频 base64 直传 → 调用阿里云 API → 返回 voice_id。"""

    async def create_voice(
        self,
        audio_bytes: bytes,
        mime_type: str,
        target_model: str,
        prefix: str,
    ) -> str:
        """调用阿里云 voice-enrollment API 创建音色（base64 直传）。

        Args:
            audio_bytes: 音频二进制数据（10-20 秒 WAV/MP3/M4A 清晰人声）
            mime_type: 音频 MIME 类型（audio/wav、audio/mpeg、audio/mp4 等）
            target_model: 绑定的目标 TTS 模型（须与后续合成模型完全一致）
            prefix: 音色名前缀（仅限字母数字，≤10 字符）

        Returns:
            复刻音色 ID（如 {target_model}-{prefix}-{唯一标识}）

        Raises:
            VoiceCloneError: 创建失败时抛出，含具体原因
        """
        data_uri = self._build_data_uri(audio_bytes, mime_type)
        cfg = self._resolve_tts_config()
        payload = {
            "model": "voice-enrollment",
            "input": {
                "action": "create_voice",
                "target_model": target_model,
                "prefix": prefix,
                "url": data_uri,
            },
        }
        result = await self._post(payload, cfg.api_key, _CREATE_TIMEOUT)

        voice_id = (result.get("output") or {}).get("voice_id", "")
        if not voice_id:
            raise VoiceCloneError(
                f"创建音色成功但未返回 voice_id: {result}"
            )
        logger.info(
            "声音复刻成功: voice_id=%s, target_model=%s, prefix=%s",
            voice_id, target_model, prefix,
        )
        return voice_id

    async def list_voices(
        self, prefix: str = "", page_index: int = 1, page_size: int = 100
    ) -> list[dict]:
        """查询当前账号下的自定义音色列表。

        Returns:
            音色列表，每项含 voice_id / gmt_create / status 等
        """
        cfg = self._resolve_tts_config()
        payload = {
            "model": "voice-enrollment",
            "input": {
                "action": "list_voice",
                "prefix": prefix,
                "page_index": page_index,
                "page_size": page_size,
            },
        }
        result = await self._post(payload, cfg.api_key, _QUERY_TIMEOUT)
        voices = (result.get("output") or {}).get("voice_list", [])
        logger.info("查询自定义音色: %d 条", len(voices))
        return voices

    async def delete_voice(self, voice_id: str) -> bool:
        """删除自定义音色。

        Returns:
            是否删除成功
        """
        cfg = self._resolve_tts_config()
        payload = {
            "model": "voice-enrollment",
            "input": {
                "action": "delete_voice",
                "voice_id": voice_id,
            },
        }
        await self._post(payload, cfg.api_key, _QUERY_TIMEOUT)
        logger.info("删除自定义音色: voice_id=%s", voice_id)
        return True

    async def preview_voice(
        self, voice_id: str, text: str = "你好，我是你的AI伙伴，很高兴见到你。"
    ) -> bytes:
        """用复刻音色合成试听音频。

        直接复用 TTSService 的合成链路，自动处理模型族匹配。

        Returns:
            音频二进制数据 (mp3)
        """
        from app.services.tts_service import TTSService

        tts = TTSService()
        return await tts.synthesize(text, voice_id=voice_id)

    # ── 内部方法 ──────────────────────────────────────────────

    @staticmethod
    def _build_data_uri(audio_bytes: bytes, mime_type: str) -> str:
        """音频字节 → base64 Data URI（data:{mime};base64,{data}）。"""
        mime = _MIME_NORMALIZE.get(mime_type.lower(), "")
        if not mime:
            raise VoiceCloneError(f"不支持的音频类型: {mime_type}")
        b64 = base64.b64encode(audio_bytes).decode("ascii")
        return f"data:{mime};base64,{b64}"

    @staticmethod
    def _resolve_tts_config() -> ResolvedModelConfig:
        """解析当前 TTS 配置，获取 API Key。"""
        return resolve_model_config("tts")

    @staticmethod
    async def _post(
        payload: dict, api_key: str, timeout: float
    ) -> dict:
        """向声音复刻 API 发送 POST 请求并解析响应。"""
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                _VOICE_ENROLLMENT_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )

        if resp.status_code != 200:
            raise VoiceCloneError(
                f"声音复刻 API 请求失败 (HTTP {resp.status_code}): "
                f"{resp.text[:300]}"
            )

        body = resp.json()
        # 阿里云 API 业务错误：code 非 20000000 或有 message
        code = body.get("code", "")
        if code and str(code) != "20000000":
            message = body.get("message", "未知错误")
            raise VoiceCloneError(f"声音复刻失败: {code} {message}")

        return body
