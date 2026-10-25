"""多媒体与 LLM 服务测试。

覆盖：
- model_registry.resolve_model_config（解析/无回退错误）
- ImageGenService：provider 识别、MIME、下载、参考图编辑、降级链
- VisionService / EmbeddingService / ASRService / TTSService（含未配置降级）
- audio_utils：_pcm_to_wav、download_qq_media
- media_storage：压缩/保存
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.config import settings
from app.llm.model_registry import (
    ResolvedModelConfig,
    _find_group,
    _group_get,
    resolve_model_config,
)


def _run(coro):
    return asyncio.run(coro)


class TestModelRegistry:
    """基础配置解析层。"""

    def _install_config(self):
        settings.model_configs = [{
            "id": "grp-1", "name": "文本组",
            "base_url": "http://x", "api_key": "sk-1", "default_model": "m-default",
        }]

    def test_resolve_success(self) -> None:
        old_cfgs = settings.model_configs
        old_cid = settings.chat_config_id
        try:
            self._install_config()
            settings.chat_config_id = "grp-1"
            cfg = resolve_model_config("chat")
            assert cfg.api_key == "sk-1" and cfg.model == "m-default"
            assert cfg.base_url == "http://x"
        finally:
            settings.model_configs = old_cfgs
            settings.chat_config_id = old_cid

    def test_resolve_uses_default_model(self) -> None:
        old_cfgs = settings.model_configs
        old_cid = settings.vision_config_id
        try:
            self._install_config()
            settings.vision_config_id = "grp-1"
            cfg = resolve_model_config("vision")
            assert cfg.model == "m-default"
        finally:
            settings.model_configs = old_cfgs
            settings.vision_config_id = old_cid

    def test_resolve_errors(self) -> None:
        old_cfgs = settings.model_configs
        old_cid = settings.chat_config_id
        try:
            # 未引用有效组
            settings.model_configs = []
            settings.chat_config_id = "no-such"
            try:
                resolve_model_config("chat")
                raise AssertionError("应抛 RuntimeError")
            except RuntimeError:
                pass

            # 组无 api_key
            settings.model_configs = [{"id": "g", "name": "无密钥", "api_key": ""}]
            settings.chat_config_id = "g"
            try:
                resolve_model_config("chat")
                raise AssertionError("应抛 RuntimeError")
            except RuntimeError:
                pass
        finally:
            settings.model_configs = old_cfgs
            settings.chat_config_id = old_cid

    def test_group_helpers(self) -> None:
        assert _group_get({"a": "1"}, "a") == "1"
        assert _group_get({"a": None}, "a", "def") == "def"
        assert _group_get(SimpleNamespace(a="2"), "a") == "2"
        assert _find_group("") is None


class TestImageGenService:
    """文生图与参考图编辑。"""

    def test_detect_provider(self) -> None:
        from app.services.imagegen_service import _detect_provider

        cfg = lambda m, b="": ResolvedModelConfig(base_url=b, api_key="k", model=m, timeout=30)  # noqa: E731
        assert _detect_provider(cfg("gemini-2.0")) == "gemini"
        assert _detect_provider(cfg("qwen-image-edit")) == "dashscope"
        assert _detect_provider(cfg("gpt-image-1", "https://api.openai.com/v1")) == "openai"

    def test_detect_image_mime(self) -> None:
        from app.services.imagegen_service import _detect_image_mime

        assert _detect_image_mime(b"\x89PNG\r\n\x1a\n") == "image/png"
        assert _detect_image_mime(b"\xff\xd8xx") == "image/jpeg"
        assert _detect_image_mime(b"RIFF0000WEBP") == "image/webp"

    def test_generate_for_task_fallback_to_plain(self) -> None:
        async def body():
            from app.services.imagegen_service import ImageGenService

            svc = ImageGenService()
            svc.generate = AsyncMock(return_value=b"img")
            task = SimpleNamespace(
                contains_self=False, ref_image_url="", prompt="p",
                fallback_prompt="fb",
            )
            out = await svc.generate_for_task(task)
            assert out == b"img"
            svc.generate.assert_awaited_with("fb")

        _run(body())

    def test_generate_for_task_reference_then_fallback_on_error(self) -> None:
        async def body():
            from app.services.imagegen_service import ImageGenService

            old = settings.imagegen_edit_enabled
            settings.imagegen_edit_enabled = True
            try:
                svc = ImageGenService()
                svc.generate_with_reference = AsyncMock(side_effect=RuntimeError("x"))
                svc.generate = AsyncMock(return_value=b"fallback")
                task = SimpleNamespace(
                    contains_self=True, ref_image_url="/api/media/1",
                    prompt="scene", fallback_prompt="fb",
                )
                out = await svc.generate_for_task(task)
                assert out == b"fallback"
            finally:
                settings.imagegen_edit_enabled = old

        _run(body())

    def test_load_reference_bytes_unsupported(self) -> None:
        async def body():
            from app.services.imagegen_service import ImageGenService

            try:
                await ImageGenService._load_reference_bytes("ftp://x")
                raise AssertionError("应抛 RuntimeError")
            except RuntimeError:
                pass

        _run(body())

    def test_edit_openai_b64(self) -> None:
        async def body():
            from app.services.imagegen_service import ImageGenService

            svc = ImageGenService()
            cfg = ResolvedModelConfig(
                base_url="http://api", api_key="k", model="gpt-image-1", timeout=30
            )

            import base64 as b64
            fake_resp = SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"data": [{"b64_json": b64.b64encode(b"pix").decode()}]},
            )

            class FakeClient:
                def __init__(self, *a, **k):
                    pass

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a):
                    return None

                async def post(self, *a, **k):
                    return fake_resp

            with patch(
                "app.services.imagegen_service.httpx.AsyncClient", FakeClient
            ):
                out = await svc._edit_openai(cfg, "p", b"ref", "image/png")
                assert out == b"pix"

        _run(body())


class TestVisionEmbeddingAsrTts:
    """VisionService / EmbeddingService / ASRService / TTSService。"""

    def test_vision_not_configured(self) -> None:
        async def body():
            from app.services.vision_service import (
                VisionService,
                get_vision_service,
                reset_vision_service,
            )

            svc = VisionService()
            with patch(
                "app.services.vision_service.resolve_model_config",
                side_effect=RuntimeError("no cfg"),
            ):
                assert await svc.describe_image("http://img") == ""

            # 单例
            reset_vision_service()
            assert get_vision_service() is get_vision_service()

        _run(body())

    def test_vision_describe_success(self) -> None:
        async def body():
            from app.services.vision_service import VisionService

            svc = VisionService()
            fake_msg = SimpleNamespace(content="一只猫")
            fake_choice = SimpleNamespace(message=fake_msg)
            fake_resp = SimpleNamespace(choices=[fake_choice])
            fake_client = SimpleNamespace(
                chat=SimpleNamespace(
                    completions=SimpleNamespace(create=AsyncMock(return_value=fake_resp))
                )
            )
            svc._client = fake_client
            svc._model = "qwen-vl"
            assert await svc.describe_image("http://img") == "一只猫"

        _run(body())

    def test_vision_describe_with_sticker_hint(self) -> None:
        """表情包预判提示词：is_likely_sticker=True 时使用专用提示词。"""
        async def body():
            from app.services.vision_service import VisionService

            svc = VisionService()
            fake_msg = SimpleNamespace(content="[表情包] 含义：开心大笑")
            fake_choice = SimpleNamespace(message=fake_msg)
            fake_resp = SimpleNamespace(choices=[fake_choice])
            fake_create = AsyncMock(return_value=fake_resp)
            fake_client = SimpleNamespace(
                chat=SimpleNamespace(
                    completions=SimpleNamespace(create=fake_create)
                )
            )
            svc._client = fake_client
            svc._model = "qwen-vl"

            result = await svc.describe_image_with_hint(
                "http://img", is_likely_sticker=True
            )
            assert result == "[表情包] 含义：开心大笑"
            # 验证使用了表情包专用提示词
            call_args = fake_create.call_args
            prompt = call_args.kwargs["messages"][0]["content"][1]["text"]
            assert "这是一张表情包/贴纸" in prompt

        _run(body())

    def test_vision_describe_with_normal_hint(self) -> None:
        """普通图片提示词：is_likely_sticker=False 时使用通用提示词。"""
        async def body():
            from app.services.vision_service import VisionService

            svc = VisionService()
            fake_msg = SimpleNamespace(content="一只猫")
            fake_choice = SimpleNamespace(message=fake_msg)
            fake_resp = SimpleNamespace(choices=[fake_choice])
            fake_create = AsyncMock(return_value=fake_resp)
            fake_client = SimpleNamespace(
                chat=SimpleNamespace(
                    completions=SimpleNamespace(create=fake_create)
                )
            )
            svc._client = fake_client
            svc._model = "qwen-vl"

            result = await svc.describe_image_with_hint(
                "http://img", is_likely_sticker=False
            )
            assert result == "一只猫"
            # 验证使用了通用提示词
            call_args = fake_create.call_args
            prompt = call_args.kwargs["messages"][0]["content"][1]["text"]
            assert "如果这是一张表情包或贴纸" in prompt

        _run(body())

    def test_embedding_disabled(self) -> None:
        async def body():
            from app.services.embedding_service import (
                EmbeddingService,
                get_embedding_service,
                reset_embedding_service,
            )

            svc = EmbeddingService()
            old = settings.embedding_enabled
            settings.embedding_enabled = False
            try:
                assert svc.enabled is False
                assert await svc.embed("text") == []
                assert await svc.embed_batch(["a", "b"]) == [[], []]
                st = svc.status()
                assert st["enabled"] is False
            finally:
                settings.embedding_enabled = old
            reset_embedding_service()
            assert get_embedding_service() is get_embedding_service()

        _run(body())

    def test_embedding_success_and_failure(self) -> None:
        async def body():
            from app.services.embedding_service import EmbeddingService

            svc = EmbeddingService()
            # 强制 enabled
            with patch.object(type(svc), "enabled", property(lambda self: True)):
                emb_item = SimpleNamespace(embedding=[0.1, 0.2], index=0)
                fake_resp = SimpleNamespace(data=[emb_item])
                svc._client = SimpleNamespace(
                    embeddings=SimpleNamespace(create=AsyncMock(return_value=fake_resp))
                )
                svc._model = "emb-m"
                out = await svc.embed("hello")
                assert out == [0.1, 0.2]

                # 批量
                batch_resp = SimpleNamespace(data=[
                    SimpleNamespace(embedding=[1.0], index=0),
                    SimpleNamespace(embedding=[2.0], index=1),
                ])
                svc._client.embeddings.create = AsyncMock(return_value=batch_resp)
                out = await svc.embed_batch(["a", "b"])
                assert out == [[1.0], [2.0]]

                # 失败记录降级
                svc._client.embeddings.create = AsyncMock(side_effect=RuntimeError("x"))
                assert await svc.embed("z") == []
                assert svc.status()["fail_count"] >= 1

        _run(body())

    def test_asr_not_configured(self) -> None:
        async def body():
            from app.services.asr_service import ASRService, _wav_sample_rate

            # WAV 采样率解析
            wav = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 12 + (24000).to_bytes(4, "little")
            assert _wav_sample_rate(wav) == 24000
            assert _wav_sample_rate(b"notwav") == 16000

            svc = ASRService()
            with patch(
                "app.services.asr_service.resolve_model_config",
                side_effect=RuntimeError("no cfg"),
            ):
                assert await svc.transcribe(b"audio") == ""

        _run(body())

    def test_tts_synthesize(self) -> None:
        async def body():
            from app.services.tts_service import TTSService

            svc = TTSService()
            with patch(
                "app.services.tts_service.resolve_model_config",
                return_value=ResolvedModelConfig(
                    base_url="", api_key="k", model="cosyvoice", timeout=30
                ),
            ), patch(
                "app.services.tts_service.asyncio.to_thread",
                new=AsyncMock(return_value=b"mp3-bytes"),
            ):
                out = await svc.synthesize("你好")
                assert out == b"mp3-bytes"

        _run(body())

    def test_tts_synthesize_with_expression(self) -> None:
        """表现力参数透传：情感标签拼入文本头部，指令与音调传给合成层。"""
        async def body():
            from app.services.tts_service import TTSService

            svc = TTSService()
            captured: dict = {}

            async def fake_to_thread(func, *args, **kwargs):
                captured["args"] = args
                return b"mp3-bytes"

            with patch(
                "app.services.tts_service.resolve_model_config",
                return_value=ResolvedModelConfig(
                    base_url="", api_key="k",
                    model="qwen-audio-3.1-tts-plus", timeout=30,
                ),
            ), patch(
                "app.services.tts_service.asyncio.to_thread", new=fake_to_thread
            ):
                out = await svc.synthesize(
                    "别难过，我在呢",
                    voice_id="longanlingxin_v3.1",
                    instruction="用温柔共情的语气轻声安慰",
                    pitch_rate=0.95,
                    emotion_tag="[empathetic]",
                )
                assert out == b"mp3-bytes"
                # 情感标签拼入合成文本头部（qwen-audio 族支持）
                assert captured["args"][2] == "[empathetic]别难过，我在呢"
                # 指令与音调透传到合成层（_call_cosyvoice 第 3/5 参）
                assert captured["args"][4] == "用温柔共情的语气轻声安慰"
                assert captured["args"][5] == 0.95

        _run(body())

    def test_tts_expression_unsupported_family_ignored(self) -> None:
        """不支持指令/情感标签的模型族：表现力参数静默忽略。"""
        async def body():
            from app.services.tts_service import TTSService

            svc = TTSService()
            captured: dict = {}

            async def fake_to_thread(func, *args, **kwargs):
                captured["args"] = args
                return b"mp3-bytes"

            with patch(
                "app.services.tts_service.resolve_model_config",
                return_value=ResolvedModelConfig(
                    base_url="", api_key="k", model="cosyvoice-v1", timeout=30,
                ),
            ), patch(
                "app.services.tts_service.asyncio.to_thread", new=fake_to_thread
            ):
                await svc.synthesize(
                    "你好",
                    voice_id="longxiaochun",
                    instruction="用轻快的语气",
                    emotion_tag="[empathetic]",
                )
                # cosyvoice-v1 不支持：指令被裁剪为空，情感标签不拼入文本
                assert captured["args"][2] == "你好"
                assert captured["args"][4] == ""

        _run(body())


class TestTTSRegistry:
    """TTS 模型族注册表：模型识别与音色兜底。"""

    def test_detect_family(self) -> None:
        from app.services.tts_registry import API_MULTIMODAL, API_WEBSOCKET, detect_family

        assert detect_family("cosyvoice-v3-flash").api_kind == API_WEBSOCKET
        assert detect_family("cosyvoice-v3-flash").name == "cosyvoice-v3"
        assert detect_family("cosyvoice-v2").name == "cosyvoice-v2"
        assert detect_family("cosyvoice-v1").name == "cosyvoice-v1"
        assert detect_family("qwen3-tts-flash").api_kind == API_MULTIMODAL
        assert detect_family("qwen-tts").api_kind == API_MULTIMODAL
        # Qwen-Audio-TTS 族：WebSocket 合成
        assert detect_family("qwen-audio-3.0-tts-plus").name == "qwen-audio"
        assert detect_family("qwen-audio-3.0-tts-plus").api_kind == API_WEBSOCKET
        # 3.1 与 3.0 音色规则不同，单独成族
        assert detect_family("qwen-audio-3.1-tts-plus").name == "qwen-audio-v31"
        assert detect_family("qwen-audio-3.1-tts-flash").name == "qwen-audio-v31"
        assert detect_family("qwen-audio-3.1-tts-plus").api_kind == API_WEBSOCKET
        # 未识别模型保守回退 v1
        assert detect_family("unknown-model").name == "cosyvoice-v1"

    def test_resolve_voice_match(self) -> None:
        from app.services.tts_registry import detect_family, resolve_voice

        v1 = detect_family("cosyvoice-v1")
        assert resolve_voice(v1, "longcheng") == ("longcheng", False)
        assert resolve_voice(v1, "longxiaochun") == ("longxiaochun", False)

        v3 = detect_family("cosyvoice-v3-flash")
        assert resolve_voice(v3, "longshange_v3") == ("longshange_v3", False)

        qwen = detect_family("qwen3-tts-flash")
        assert resolve_voice(qwen, "Cherry") == ("Cherry", False)
        assert resolve_voice(qwen, "Ethan") == ("Ethan", False)
        # Qwen-TTS 复刻音色为小写自定义名，直接放行
        assert resolve_voice(qwen, "guanyu") == ("guanyu", False)

        qa = detect_family("qwen-audio-3.0-tts-plus")
        # Qwen-Audio 带版本号预置音色与复刻音色均匹配
        assert resolve_voice(qa, "longanhuan_v3.6") == ("longanhuan_v3.6", False)
        # 族默认音色直接放行（不触发回退告警）
        assert resolve_voice(qa, qa.default_voice) == (qa.default_voice, False)
        assert resolve_voice(qa, "qwen-audio-3.0-tts-plus-myvoice-abc") == (
            "qwen-audio-3.0-tts-plus-myvoice-abc", False,
        )

        v31 = detect_family("qwen-audio-3.1-tts-plus")
        # 3.1 系统音色白名单与默认音色直接放行
        for voice in (
            "longanlingxin_v3.1",
            "longanqian_v3.1",
            "longanhuan_v3.1",
            "longanfengyue_v3.1",
            "xunanchuan_v3.1",
        ):
            assert resolve_voice(v31, voice) == (voice, False)
        assert resolve_voice(v31, v31.default_voice) == (v31.default_voice, False)
        # 3.0 基础/复刻音色实测可跨代使用
        assert resolve_voice(v31, "qwen-audio-3.0-tts-plus-myvoice-abc") == (
            "qwen-audio-3.0-tts-plus-myvoice-abc", False,
        )
        assert resolve_voice(v31, "qwen-audio-3.0-tts-plus-longyufengmo") == (
            "qwen-audio-3.0-tts-plus-longyufengmo", False,
        )
        # 3.0 代系统音色自动映射到 3.1 可用音色
        assert resolve_voice(v31, "longanlingxin") == ("longanlingxin_v3.1", False)
        assert resolve_voice(v31, "longanqian") == ("longanqian_v3.1", False)
        assert resolve_voice(v31, "longanhuan_v3.6") == ("longanhuan_v3.1", False)
        assert resolve_voice(v31, "longanlufeng") == ("xunanchuan_v3.1", False)

    def test_resolve_voice_fallback(self) -> None:
        from app.services.tts_registry import detect_family, resolve_voice

        v1 = detect_family("cosyvoice-v1")
        # v3 音色配 v1 模型 → 回退 v1 默认音色
        voice, fb = resolve_voice(v1, "longshange_v3")
        assert fb is True and voice == v1.default_voice
        # v2 音色配 v1 模型 → 回退
        voice, fb = resolve_voice(v1, "longxiaochun_v2")
        assert fb is True and voice == v1.default_voice

        v3 = detect_family("cosyvoice-v3-flash")
        # v1 音色配 v3 模型 → 回退
        voice, fb = resolve_voice(v3, "longxiaochun")
        assert fb is True and voice == v3.default_voice

        qwen = detect_family("qwen3-tts-flash")
        # CosyVoice 音色配 Qwen 模型 → 回退 Qwen 默认音色
        voice, fb = resolve_voice(qwen, "longshange_v3")
        assert fb is True and voice == qwen.default_voice

        qa = detect_family("qwen-audio-3.0-tts-plus")
        # CosyVoice v1 裸音色配 Qwen-Audio 模型 → 回退（避免 Engine error 411）
        for bad in ("longcheng", "longxiaochun", "longshu"):
            voice, fb = resolve_voice(qa, bad)
            assert fb is True and voice == qa.default_voice
        # CosyVoice v2/v3 音色配 Qwen-Audio 模型 → 回退
        voice, fb = resolve_voice(qa, "longxiaochun_v2")
        assert fb is True and voice == qa.default_voice

        v31 = detect_family("qwen-audio-3.1-tts-plus")
        # CosyVoice 音色与 3.1 不支持的系统音色 → 回退默认音色
        for bad in (
            "longcheng",
            "longxiaochun_v2",
            "cosyvoice-v3-myvoice-abc",
            "longanlingxi_v3.1",
            "longanxiaoxin_v3.1",
        ):
            voice, fb = resolve_voice(v31, bad)
            assert fb is True and voice == v31.default_voice

    def test_resolve_voice_empty_and_clone(self) -> None:
        from app.services.tts_registry import detect_family, resolve_voice

        v1 = detect_family("cosyvoice-v1")
        # 空音色 → 用默认，不算回退
        voice, fb = resolve_voice(v1, "")
        assert fb is False and voice == v1.default_voice
        # 复刻/设计的自定义音色 ID 直接放行
        voice, fb = resolve_voice(v1, "cosyvoice-v1-myvoice-abc123")
        assert fb is False and voice == "cosyvoice-v1-myvoice-abc123"

        v31 = detect_family("qwen-audio-3.1-tts-plus")
        # 3.1 空音色 → 用 3.1 默认音色
        voice, fb = resolve_voice(v31, "")
        assert fb is False and voice == "longanlingxin_v3.1"

    def test_family_capabilities(self) -> None:
        """模型族表现力能力标志：指令控制与情感标签按族声明。"""
        from app.services.tts_registry import detect_family, supports_instruction

        qa = detect_family("qwen-audio-3.0-tts-plus")
        assert qa.supports_instruction and qa.supports_emotion_tags
        assert supports_instruction(qa, "qwen-audio-3.0-tts-plus")

        v31 = detect_family("qwen-audio-3.1-tts-plus")
        assert v31.supports_instruction and v31.supports_emotion_tags
        assert supports_instruction(v31, "qwen-audio-3.1-tts-plus")

        v3 = detect_family("cosyvoice-v3-flash")
        assert v3.supports_instruction and not v3.supports_emotion_tags

        v1 = detect_family("cosyvoice-v1")
        assert not v1.supports_instruction and not v1.supports_emotion_tags
        assert not supports_instruction(v1, "cosyvoice-v1")

        # qwen-tts 族内仅 instruct 系列支持指令（按模型名运行时判定）
        qt = detect_family("qwen3-tts-instruct-flash")
        assert supports_instruction(qt, "qwen3-tts-instruct-flash")
        assert not supports_instruction(qt, "qwen3-tts-flash")


class TestTTSExpression:
    """TTS 表现力映射：情绪/心情 → 指令文案/情感标签/音调。"""

    @staticmethod
    def _mood(valence: float, arousal: float = 0.5):
        from app.models.service_memory import CharacterMood

        return CharacterMood(valence=valence, arousal=arousal)

    def test_instruction_by_identity_emotion(self) -> None:
        from app.services.tts_expression import build_tts_instruction

        # 身份情绪优先，且语速描述内嵌文案（替代数值语速参数）
        assert "安慰" in build_tts_instruction(None, "sad")
        assert "语速" in build_tts_instruction(None, "sad")
        assert "笑意" in build_tts_instruction(None, "happy")
        # 无情绪且无心情 → 不传指令（走自然音色）
        assert build_tts_instruction(None, None) == ""

    def test_instruction_by_mood(self) -> None:
        from app.services.tts_expression import build_tts_instruction

        assert "低落" in build_tts_instruction(self._mood(-0.6), None)
        assert "轻快" in build_tts_instruction(self._mood(0.6), None)
        # 平静心情 → 不传指令
        assert build_tts_instruction(self._mood(0.05), None) == ""

    def test_instruction_length_limit(self) -> None:
        from app.services.tts_expression import (
            _instruction_len,
            build_tts_instruction,
        )

        # 所有组合的指令文案均不超官方 100 字符上限（汉字按 2 计）
        for emo in ("sad", "angry", "anxious", "happy", "lonely", None):
            for valence in (-0.9, -0.2, 0.0, 0.2, 0.9):
                inst = build_tts_instruction(self._mood(valence), emo)
                assert _instruction_len(inst) <= 100

    def test_emotion_tag(self) -> None:
        from app.services.tts_expression import pick_emotion_tag

        assert pick_emotion_tag(None, "sad") == "[empathetic]"
        assert pick_emotion_tag(None, "lonely") == "[empathetic]"
        assert pick_emotion_tag(None, "angry") == "[serious]"
        assert pick_emotion_tag(None, "happy") == "[excited]"
        assert pick_emotion_tag(None, None) == ""

    def test_pitch_modifier(self) -> None:
        from app.services.tts_expression import mood_pitch_modifier

        assert mood_pitch_modifier(None) == 1.0
        assert mood_pitch_modifier(self._mood(0.05)) == 1.0
        assert mood_pitch_modifier(self._mood(-0.8)) < 1.0
        assert mood_pitch_modifier(self._mood(0.8)) > 1.0

    def test_mood_rate_modifier(self) -> None:
        from app.services.tts_expression import mood_rate_modifier

        assert mood_rate_modifier(None) == ""
        assert mood_rate_modifier(self._mood(0.05)) == ""
        assert "放慢" in mood_rate_modifier(self._mood(-0.6))
        assert "稍快" in mood_rate_modifier(self._mood(0.6))


class TestAudioUtils:
    """音频工具。"""

    def test_pcm_to_wav_header(self) -> None:
        from app.services.audio_utils import _pcm_to_wav

        wav = _pcm_to_wav(b"\x00\x01" * 10, sample_rate=24000)
        assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"

    def test_download_qq_media(self) -> None:
        async def body():
            from app.services import audio_utils

            fake_resp = SimpleNamespace(
                raise_for_status=lambda: None, content=b"media-bytes"
            )

            class FakeClient:
                def __init__(self, *a, **k):
                    pass

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a):
                    return None

                async def get(self, *a, **k):
                    return fake_resp

            with patch.object(audio_utils.httpx, "AsyncClient", FakeClient):
                out = await audio_utils.download_qq_media("http://x")
                assert out == b"media-bytes"

        _run(body())


class TestMediaStorage:
    """媒体存储。"""

    def test_compress_small_image_noop(self) -> None:
        from app.services.media_storage import _compress_image

        data = b"small-image-data"
        out, ct = _compress_image(data, "image/png")
        assert out == data and ct == "image/png"

    def test_save_image_from_bytes(self) -> None:
        async def body():
            import mongomock_motor

            from app.db import mongo
            from app.services import media_storage

            mongo._db = mongomock_motor.AsyncMongoMockClient()["test_media_store"]
            try:
                url = await media_storage.save_image_from_bytes(b"img", "image/png")
                assert url.startswith("/api/media/")
            finally:
                mongo._db = None

        _run(body())

    def test_save_image_from_url(self) -> None:
        async def body():
            import mongomock_motor

            from app.db import mongo
            from app.services import media_storage

            mongo._db = mongomock_motor.AsyncMongoMockClient()["test_media_url"]
            fake_resp = SimpleNamespace(
                raise_for_status=lambda: None,
                content=b"downloaded",
                headers={"content-type": "image/png"},
            )

            class FakeClient:
                def __init__(self, *a, **k):
                    pass

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a):
                    return None

                async def get(self, *a, **k):
                    return fake_resp

            try:
                with patch.object(media_storage.httpx, "AsyncClient", FakeClient):
                    url = await media_storage.save_image_from_url("http://x/a.png")
                    assert url.startswith("/api/media/")
            finally:
                mongo._db = None

        _run(body())
