"""图形验证码服务：算式图片 + 内存答案表。

用已声明依赖 `pillow` 生成"两位数加减法"算式图，答案存带 TTL 的内存 map 并一次性消费：
- 答对或答错都立即失效，攻击者无法对同一验证码批量试错；
- 单实例内存实现，多实例部署需换共享存储（同限流器的 Redis 约束）。
"""

import logging
import random
import secrets
import time
from collections import deque
from io import BytesIO
from typing import Any

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# 答案有效期（秒）与内存中最多保留的验证码条数
CAPTCHA_TTL_SECONDS = 300
_MAX_PENDING = 5000

# 图片尺寸与配色（灰底深字，无需字体文件，随包零资源依赖）
_IMG_WIDTH = 130
_IMG_HEIGHT = 44


class CaptchaService:
    """生成与校验图形验证码。"""

    def __init__(self) -> None:
        # {captcha_id: (answer, expire_monotonic)}
        self._answers: dict[str, int] = {}
        self._expiries: dict[str, float] = {}
        self._order: deque[str] = deque()

    def generate(self) -> tuple[str, str]:
        """生成验证码。

        Returns:
            (captcha_id, data_uri)：data_uri 可直接用作 `<img src>`
        """
        self._purge()
        a, b = random.randint(10, 99), random.randint(1, 9)
        operator = random.choice(("+", "-"))
        answer = a + b if operator == "+" else a - b
        captcha_id = secrets.token_urlsafe(16)
        self._answers[captcha_id] = answer
        self._expiries[captcha_id] = time.monotonic() + CAPTCHA_TTL_SECONDS
        self._order.append(captcha_id)
        if len(self._order) > _MAX_PENDING:
            self._answers.pop(self._order.popleft(), None)
        return captcha_id, self._render(f"{a} {operator} {b} = ?")

    def verify(self, captcha_id: str, answer: str) -> bool:
        """校验答案：无论对错都立即消费（一次性），不存在或过期一律失败。"""
        if not captcha_id:
            return False
        expected = self._answers.pop(captcha_id, None)
        expire = self._expiries.pop(captcha_id, None)
        if expected is None or expire is None:
            return False
        if expire < time.monotonic():
            return False
        try:
            return int(answer.strip()) == expected
        except (AttributeError, ValueError):
            return False

    def reset(self) -> None:
        """清空全部答案（测试隔离与运维手动清理）。"""
        self._answers.clear()
        self._expiries.clear()
        self._order.clear()

    def _purge(self) -> None:
        """回收已过期答案，避免长时间运行累积。"""
        now = time.monotonic()
        expired = [cid for cid, exp in self._expiries.items() if exp < now]
        for cid in expired:
            self._answers.pop(cid, None)
            self._expiries.pop(cid, None)
            if cid in self._order:
                self._order.remove(cid)

    @staticmethod
    def _render(text: str) -> str:
        """把算式渲染成 PNG 并编码为 base64 data URI，加干扰线降低 OCR 命中率。"""
        # Pillow 桩未把 FreeTypeFont 声明为 ImageFont 子类，两者并存故取 Any
        font: Any
        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", 20)
        except OSError:
            # 系统字体缺失时回落 PIL 内置位图字体（不影响可辨识度）
            font = ImageFont.load_default()
        img = Image.new("RGB", (_IMG_WIDTH, _IMG_HEIGHT), (245, 246, 248))
        draw = ImageDraw.Draw(img)
        for _ in range(4):
            x1, y1 = random.randint(0, _IMG_WIDTH), random.randint(0, _IMG_HEIGHT)
            x2, y2 = random.randint(0, _IMG_WIDTH), random.randint(0, _IMG_HEIGHT)
            draw.line((x1, y1, x2, y2), fill=(190, 195, 205), width=1)
        draw.text((12, 10), text, font=font, fill=(40, 45, 60))
        buf = BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + _b64(buf.getvalue())


def _b64(raw: bytes) -> str:
    """字节串转 base64 文本。"""
    import base64

    return base64.b64encode(raw).decode()


# 模块级单例：验证码答案表跨请求共享
captcha_service = CaptchaService()
