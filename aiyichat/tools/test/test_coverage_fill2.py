"""收尾批 2：补齐 bot_config 加载、audio 转换、图片压缩的覆盖。"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import mongomock_motor

from app.db import mongo


def _run(coro):
    return asyncio.run(coro)



class TestBotConfigLoader:
    """从 MongoDB 加载 Bot 配置。"""

    def test_load_bots_config(self) -> None:
        async def body():
            mongo._db = mongomock_motor.AsyncMongoMockClient()["test_botcfg"]
            try:
                from app.bot_config import async_load_bots_config
                from app.db.repositories.bot_config_repo import BotConfigRepository
                from app.models.bot_config import BotConfigCreate

                # 空库
                assert await async_load_bots_config() == []

                # 有配置
                await BotConfigRepository().create(
                    BotConfigCreate(account_id="acc-1", name="Bot甲", app_id="a1")
                )
                configs = await async_load_bots_config()
                assert len(configs) == 1 and configs[0].name == "Bot甲"
            finally:
                mongo._db = None

        _run(body())


class TestAudioConversions:
    """silk/wav/mp3 音频转换（注入伪 pilk + mock ffmpeg）。"""

    def _install_fake_pilk(self):
        """注入伪 pilk 模块：decode/encode 写出目标文件并返回时长。"""
        fake = SimpleNamespace()

        def _decode(src, dst, **kw):
            Path(dst).write_bytes(b"\x00\x01" * 100)
            return 1000

        def _encode(src, dst, **kw):
            Path(dst).write_bytes(b"SILKDATA")
            return 1000

        fake.decode = _decode
        fake.encode = _encode
        return fake

    def test_silk_to_wav(self) -> None:
        async def body():
            from app.services import audio_utils

            fake_pilk = self._install_fake_pilk()
            with patch.dict(sys.modules, {"pilk": fake_pilk}):
                wav = await audio_utils.silk_to_wav(b"silk-input")
                assert wav[:4] == b"RIFF"

        _run(body())

    def test_wav_to_silk(self) -> None:
        async def body():
            from app.services import audio_utils

            fake_pilk = self._install_fake_pilk()
            fake_subprocess = SimpleNamespace(
                run=lambda *a, **k: SimpleNamespace(returncode=0)
            )
            with patch.dict(
                sys.modules, {"pilk": fake_pilk, "subprocess": fake_subprocess}
            ):
                silk = await audio_utils.wav_to_silk(b"wav-input")
                assert silk == b"SILKDATA"

        _run(body())


class TestMediaStorageCompress:
    """媒体压缩：超限图片走 Pillow 压缩链。"""

    def test_compress_large_image(self) -> None:
        from app.services.media_storage import _compress_image

        # 构造一张较大的真实 PNG，设定极小 max_size 强制走压缩分支
        from io import BytesIO

        from PIL import Image

        img = Image.new("RGB", (400, 400), color=(120, 60, 30))
        buf = BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()

        out, ct = _compress_image(data, "image/png", max_size=100)
        assert ct == "image/jpeg" and len(out) > 0
