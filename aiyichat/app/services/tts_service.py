"""TTS 语音合成服务 -- 阿里云百炼（CosyVoice / Qwen-TTS 多模型族）。"""

import asyncio
import logging

import httpx

from app.llm.model_registry import ResolvedModelConfig, resolve_model_config
from app.services.tts_registry import (
    API_MULTIMODAL,
    TTSModelFamily,
    detect_family,
    resolve_voice,
    supports_instruction,
)

logger = logging.getLogger(__name__)

# Qwen-TTS 返回的音频 URL 下载超时（秒）
_AUDIO_DOWNLOAD_TIMEOUT = 30.0


class TTSService:
    """文字转语音：文本 + 音色 + 表现力参数（指令/情感标签/音调）-> 音频 bytes。"""

    async def synthesize(
        self,
        text: str,
        voice_id: str = "longxiaochun",
        instruction: str = "",
        pitch_rate: float = 1.0,
        emotion_tag: str = "",
    ) -> bytes:
        """
        将文字合成为语音。

        Args:
            text: 待合成文本（情感标签由本方法按模型族能力自动拼入头部）
            voice_id: 音色ID（克隆音色或预置音色），与当前模型族不匹配时自动回退族默认音色
            instruction: 自然语言指令（控制语气/情感/语速），模型不支持时自动忽略；
                指令失败时自动去指令重试一次（防参数不兼容）
            pitch_rate: 音调倍率 (0.5~2.0, 默认1.0)，Qwen-TTS 族不支持会自动忽略
            emotion_tag: 情感标签（如 [empathetic]），仅 qwen-audio 族生效，
                实际合成前拼入文本头部，不污染调用方的原始文本

        Returns:
            音频二进制数据 (mp3/wav 格式)，失败抛出异常
        """
        cfg = resolve_model_config("tts")  # 未正确配置时抛 RuntimeError
        family = detect_family(cfg.model)
        voice, fell_back = resolve_voice(family, voice_id)
        if fell_back:
            logger.warning(
                "音色 %r 与模型族 %s 不匹配，回退为默认音色 %r",
                voice_id,
                family.name,
                voice,
            )

        # 表现力参数按模型族能力裁剪：不支持时静默忽略（优雅降级）
        eff_instruction = (
            instruction.strip() if supports_instruction(family, cfg.model) else ""
        )
        synth_text = text
        if emotion_tag and family.supports_emotion_tags:
            synth_text = emotion_tag + text

        try:
            if family.api_kind == API_MULTIMODAL:
                audio = await asyncio.to_thread(
                    self._call_qwen, cfg, family, synth_text, voice, eff_instruction
                )
            else:
                audio = await asyncio.to_thread(
                    self._call_cosyvoice,
                    cfg,
                    family,
                    synth_text,
                    voice,
                    eff_instruction,
                    pitch_rate,
                )
        except Exception:
            # 带指令合成失败时去掉指令重试一次（防个别模型指令参数不兼容）
            if not eff_instruction:
                raise
            logger.warning(
                "TTS 指令合成失败，去指令重试 (model=%s)", cfg.model, exc_info=True
            )
            if family.api_kind == API_MULTIMODAL:
                audio = await asyncio.to_thread(
                    self._call_qwen, cfg, family, synth_text, voice, ""
                )
            else:
                audio = await asyncio.to_thread(
                    self._call_cosyvoice, cfg, family, synth_text, voice, "", pitch_rate
                )

        logger.info(
            "TTS 合成成功: %d bytes, model=%s, voice=%s, instruction=%s, text=%s",
            len(audio),
            cfg.model,
            voice,
            eff_instruction or "(无)",
            text[:40],
        )
        return audio

    def _call_cosyvoice(
        self,
        cfg: ResolvedModelConfig,
        family: TTSModelFamily,
        text: str,
        voice: str,
        instruction: str,
        pitch_rate: float,
    ) -> bytes:
        """CosyVoice/Qwen-Audio 族：WebSocket SpeechSynthesizer，直接返回 mp3 字节。

        语速不再使用数值参数，由 instruction 文案内的自然语言描述控制。
        """
        import dashscope
        from dashscope.audio.tts_v2 import SpeechSynthesizer

        dashscope.api_key = cfg.api_key

        # 表现力参数仅在非默认值时传入，避免覆盖 SDK/服务端默认行为
        kwargs: dict[str, float | str | None] = {}
        if instruction:
            kwargs["instruction"] = instruction
        if pitch_rate != 1.0:
            kwargs["pitch_rate"] = max(0.5, min(2.0, pitch_rate))
        synthesizer = SpeechSynthesizer(
            model=cfg.model,
            voice=voice,
            **kwargs,
        )
        try:
            audio_data = synthesizer.call(text)
        except AttributeError:
            # 防御 websocket-client 1.9.x 兼容 bug：连接已被服务端关闭时
            # SDK 清理阶段报 'NoneType' 无 close_frame，但音频实际已收全，
            # 此时返回已收数据避免误伤（配合 requirements 固定版本双保险）
            audio_data = getattr(synthesizer, "_audio_data", None)
            if not audio_data:
                raise
        if not audio_data:
            # SDK 会把服务端真实错误（如 AllocationQuota.FreeTierOnly）吞掉后
            # 伪装成 ConnectionError 或返回空，这里主动透出最近一次错误
            last_err = getattr(synthesizer, "last_error", None) or getattr(
                synthesizer, "_last_error", None
            )
            detail = f"，SDK 最近错误: {last_err}" if last_err else ""
            raise RuntimeError(
                f"TTS 合成返回空数据 (model={cfg.model}, voice={voice}){detail}；"
                "常见原因：模型免费额度耗尽(FreeTierOnly)或音色与模型不匹配"
            )
        return audio_data

    def _call_qwen(
        self,
        cfg: ResolvedModelConfig,
        family: TTSModelFamily,
        text: str,
        voice: str,
        instruction: str,
    ) -> bytes:
        """Qwen-TTS 族：HTTP MultiModalConversation，返回音频 URL 后下载。

        该族不支持音调参数；仅 instruct 系列支持指令控制（参数名
        instructions，与 CosyVoice 的 instruction 不同）。
        """
        import dashscope

        call_kwargs: dict[str, str] = {
            "model": cfg.model,
            "api_key": cfg.api_key,
            "text": text,
            "voice": voice,
        }
        if instruction:
            call_kwargs["instructions"] = instruction
        resp = dashscope.MultiModalConversation.call(**call_kwargs)
        status = getattr(resp, "status_code", 0)
        if status != 200:
            code = getattr(resp, "code", "")
            message = getattr(resp, "message", "")
            raise RuntimeError(
                f"Qwen-TTS 合成失败 (model={cfg.model}, voice={voice}): "
                f"{code} {message}"
            )
        audio = (resp.output or {}).get("audio", {})
        url = audio.get("url", "")
        if not url:
            raise RuntimeError(
                f"Qwen-TTS 未返回音频 URL (model={cfg.model}, voice={voice})"
            )
        return self._download_audio(url)

    @staticmethod
    def _download_audio(url: str) -> bytes:
        """下载 Qwen-TTS 返回的音频 URL（24h 有效的 OSS 地址）。"""
        with httpx.Client(timeout=_AUDIO_DOWNLOAD_TIMEOUT) as client:
            resp = client.get(url)
            resp.raise_for_status()
            if not resp.content:
                raise RuntimeError("下载的音频数据为空")
            return resp.content
