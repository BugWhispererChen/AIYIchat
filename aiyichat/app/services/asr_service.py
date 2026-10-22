"""ASR 语音转文字服务 — 阿里云百炼 Paraformer。"""

import asyncio
import logging
import tempfile
from http import HTTPStatus
from pathlib import Path
from typing import Any

from app.llm.model_registry import resolve_model_config

logger = logging.getLogger(__name__)


def _wav_sample_rate(data: bytes, default: int = 16000) -> int:
    """从 WAV 头解析采样率（字节 24-28，小端）；非标准 WAV 时返回 default。"""
    if len(data) >= 28 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return int.from_bytes(data[24:28], "little")
    return default


class ASRService:
    """语音转文字：WAV bytes → 文本。"""

    async def transcribe(self, audio_data: bytes, fmt: str = "wav") -> str:
        """
        将音频转为文字。

        Args:
            audio_data: WAV 格式音频字节
            fmt: 音频格式标识

        Returns:
            识别出的文本，失败返回空字符串
        """
        try:
            cfg = resolve_model_config("asr")
        except RuntimeError as e:
            logger.warning("ASR 未正确配置: %s", e)
            return ""

        def _call() -> str:
            import dashscope
            from dashscope.audio.asr import Recognition

            dashscope.api_key = cfg.api_key

            # 写临时文件供 SDK 使用
            with tempfile.NamedTemporaryFile(
                suffix=f".{fmt}", delete=False
            ) as f:
                f.write(audio_data)
                tmp_path = f.name

            # WAV 自带采样率信息，从文件头解析以匹配实际音频（微信 silk 转出为 24kHz），
            # 避免声明采样率与真实音频不一致导致识别失真
            sr = _wav_sample_rate(audio_data) if fmt == "wav" else 16000
            try:
                # SDK 存根把 callback 声明成必填的 RecognitionCallback，而本项目走同步
                # `call()` 不订阅流式回调，传 None 是既有行为。用 Any 变量承载这个
                # "无回调"，既不改调用方式也不改运行行为。
                no_callback: Any = None
                # SDK 对象的属性与回调按其自身存根定义（本项目只读 status_code/句子列表），
                # 标注 Any 限于这条第三方边界，下面的 getattr/try 降级链一字未动
                recognition: Any = Recognition(
                    model=cfg.model,
                    format=fmt,
                    sample_rate=sr,
                    language_hints=["zh", "en"],
                    callback=no_callback,
                )
                result: Any = recognition.call(tmp_path)
                if result.status_code == HTTPStatus.OK:
                    sentences = result.get_sentence()
                    if sentences:
                        return "".join(
                            s.get("text", "") for s in sentences
                        )
                else:
                    logger.warning(
                        "ASR 返回非200: %s %s",
                        result.status_code,
                        getattr(result, "message", ""),
                    )
                return ""
            finally:
                Path(tmp_path).unlink(missing_ok=True)

        try:
            text = await asyncio.to_thread(_call)
            if text:
                logger.info("ASR 识别成功: %s", text[:50])
            return text
        except Exception as e:
            logger.error("ASR 识别失败: %s", e)
            return ""
