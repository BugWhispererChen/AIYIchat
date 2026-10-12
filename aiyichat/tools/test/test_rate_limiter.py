"""限频器单元测试。

RateLimiter 从 settings.rate_limit_per_user 读取上限，
测试中通过 mock.patch 注入不同限额值来验证行为。
"""

from unittest.mock import patch

from app.utils.rate_limiter import RateLimiter


class TestRateLimiter:
    """RateLimiter 功能测试集。"""

    @patch("app.utils.rate_limiter.settings")
    def test_allows_within_limit(self, mock_settings) -> None:
        """在限额内的请求应被放行。"""
        mock_settings.rate_limit_per_user = 5
        limiter = RateLimiter()
        for _ in range(5):
            assert limiter.is_allowed("user1") is True

    @patch("app.utils.rate_limiter.settings")
    def test_blocks_over_limit(self, mock_settings) -> None:
        """超出限额的请求应被阻止。"""
        mock_settings.rate_limit_per_user = 3
        limiter = RateLimiter()
        for _ in range(3):
            limiter.is_allowed("user1")
        assert limiter.is_allowed("user1") is False

    @patch("app.utils.rate_limiter.settings")
    def test_separate_users(self, mock_settings) -> None:
        """不同用户的限额互相独立。"""
        mock_settings.rate_limit_per_user = 2
        limiter = RateLimiter()
        limiter.is_allowed("user1")
        limiter.is_allowed("user1")
        assert limiter.is_allowed("user1") is False
        assert limiter.is_allowed("user2") is True

    @patch("app.utils.rate_limiter.settings")
    def test_get_remaining(self, mock_settings) -> None:
        """get_remaining 应返回正确的剩余次数。"""
        mock_settings.rate_limit_per_user = 5
        limiter = RateLimiter()
        assert limiter.get_remaining("user1") == 5
        limiter.is_allowed("user1")
        limiter.is_allowed("user1")
        assert limiter.get_remaining("user1") == 3
