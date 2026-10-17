"""限流器与验证码服务单元测试。"""

import time

import pytest

from app.services.captcha_service import CAPTCHA_TTL_SECONDS, CaptchaService
from app.utils.rate_limit import (
    RateLimited,
    SlidingWindowLimiter,
    check_rate_limit,
    login_limiter,
    register_limiter,
)


class TestSlidingWindowLimiter:
    """IP 滑动窗口限流测试集。"""

    def test_allows_up_to_limit(self) -> None:
        """窗口内放行到上限，第 N+1 次拒绝。"""
        limiter = SlidingWindowLimiter()
        for _ in range(3):
            assert limiter.allow("198.51.100.23|register", 3, 60.0) is True
        assert limiter.allow("198.51.100.23|register", 3, 60.0) is False

    def test_keys_are_isolated(self) -> None:
        """不同 IP / 不同作用域互不影响。"""
        limiter = SlidingWindowLimiter()
        assert limiter.allow("a|register", 1, 60.0) is True
        assert limiter.allow("a|register", 1, 60.0) is False
        assert limiter.allow("b|register", 1, 60.0) is True
        assert limiter.allow("a|login", 1, 60.0) is True

    def test_window_slides(self, monkeypatch) -> None:
        """窗口滑出后重新放行（用可控时钟，避免 sleep 在 Windows 上抖动）。"""
        real = time.monotonic
        state = {"offset": 0.0}
        monkeypatch.setattr("app.utils.rate_limit.time.monotonic", lambda: real() + state["offset"])
        limiter = SlidingWindowLimiter()
        assert limiter.allow("ip", 1, 60.0) is True
        assert limiter.allow("ip", 1, 60.0) is False
        state["offset"] = 59.0  # 仍在窗口内
        assert limiter.allow("ip", 1, 60.0) is False
        state["offset"] = 61.0  # 首条记录滑出窗口
        assert limiter.allow("ip", 1, 60.0) is True

    def test_zero_limit_disables(self) -> None:
        """limit<=0 表示不限流（开关语义）。"""
        limiter = SlidingWindowLimiter()
        for _ in range(50):
            assert limiter.allow("ip", 0, 60.0) is True
            assert limiter.allow("ip", -1, 60.0) is True

    def test_retry_after(self) -> None:
        """剩余等待秒数向上取整且至少为 1，未限流的 key 返回 0。"""
        limiter = SlidingWindowLimiter()
        assert limiter.retry_after("ip", 60.0) == 0
        limiter.allow("ip", 1, 60.0)
        assert 1 <= limiter.retry_after("ip", 60.0) <= 60

    def test_reset_key_and_all(self) -> None:
        """按 key 复位与整体复位。"""
        limiter = SlidingWindowLimiter()
        limiter.allow("a", 1, 60.0)
        limiter.allow("b", 1, 60.0)
        assert limiter.allow("a", 1, 60.0) is False
        limiter.reset("a")
        assert limiter.allow("a", 1, 60.0) is True
        limiter.reset()
        assert limiter.allow("b", 1, 60.0) is True

    def test_max_keys_eviction(self) -> None:
        """key 总量受上限约束，超出时淘汰最早写入的 key（防内存被伪造 IP 撑爆）。"""
        limiter = SlidingWindowLimiter(max_keys=3)
        for i in range(5):
            assert limiter.allow(f"ip{i}", 5, 60.0) is True
        assert len(limiter._hits) <= 3

    def test_check_rate_limit_scopes(self) -> None:
        """check_rate_limit 走注册/登录两个单例，作用域与阈值互不串用。"""
        register_limiter.reset()
        login_limiter.reset()
        check_rate_limit("register", "198.51.100.77", 1)
        with pytest.raises(RateLimited) as err:
            check_rate_limit("register", "198.51.100.77", 1)
        assert err.value.scope == "register"
        assert err.value.retry_after >= 1
        # 登录通道不受注册计数影响
        check_rate_limit("login", "198.51.100.77", 1)

    def test_unknown_scope_rejected(self) -> None:
        """未登记的作用域属于编码错误，直接抛 ValueError。"""
        with pytest.raises(ValueError):
            check_rate_limit("spam", "198.51.100.77", 1)


class TestCaptchaService:
    """验证码生成与消费测试集。"""

    def test_generate_shape(self) -> None:
        """生成返回 ID 与 PNG data URI，答案为两位数加减法结果。"""
        service = CaptchaService()
        cid, image = service.generate()
        assert cid and len(cid) >= 16
        assert image.startswith("data:image/png;base64,")
        answer = service._answers[cid]
        assert -99 <= answer <= 199
        assert CAPTCHA_TTL_SECONDS > 0

    def test_verify_consumes_once(self) -> None:
        """答对即消费，同一 ID 不可复用。"""
        service = CaptchaService()
        cid, _ = service.generate()
        answer = str(service._answers[cid])
        assert service.verify(cid, answer) is True
        assert service.verify(cid, answer) is False

    def test_wrong_answer_consumes(self) -> None:
        """答错同样立即失效，无法对同一验证码批量试错。"""
        service = CaptchaService()
        cid, _ = service.generate()
        assert service.verify(cid, "wrong") is False
        assert service.verify(cid, str(service._answers.get(cid, -1))) is False
        assert cid not in service._answers

    def test_missing_or_empty_id(self) -> None:
        """空 ID 与未知 ID 一律失败。"""
        service = CaptchaService()
        assert service.verify("", "1") is False
        assert service.verify("never-issued", "1") is False

    def test_expiry(self, monkeypatch) -> None:
        """超过 TTL 的答案失效。"""
        real = time.monotonic
        state = {"offset": 0.0}
        monkeypatch.setattr(time, "monotonic", lambda: real() + state["offset"])
        service = CaptchaService()
        cid, _ = service.generate()
        answer = str(service._answers[cid])
        state["offset"] = CAPTCHA_TTL_SECONDS + 1
        assert service.verify(cid, answer) is False

    def test_purge_and_reset(self, monkeypatch) -> None:
        """过期项在下次生成时被回收；reset 清空全部。"""
        real = time.monotonic
        state = {"offset": 0.0}
        monkeypatch.setattr(time, "monotonic", lambda: real() + state["offset"])
        service = CaptchaService()
        cid, _ = service.generate()
        state["offset"] = CAPTCHA_TTL_SECONDS + 1
        service.generate()
        assert cid not in service._answers
        cid2, _ = service.generate()
        service.reset()
        assert service._answers == {}
        assert service.verify(cid2, "0") is False

    def test_answer_is_int(self) -> None:
        """减法结果可能为负数，字符串空格容错。"""
        service = CaptchaService()
        for _ in range(20):
            cid, _ = service.generate()
            expected = service._answers[cid]
            assert isinstance(expected, int)
            assert service.verify(cid, f"  {expected} ") is True
