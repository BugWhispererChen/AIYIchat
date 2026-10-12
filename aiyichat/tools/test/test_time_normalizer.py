"""jionlp 相对时间归一化封装（time_normalizer.py）单元测试。

覆盖方案 P2 的降级与边界：
- jionlp 缺失（mock 导入失败）→ 全链路降级，返回 None/空，不阻断主链路
- jionlp 抛 ValueError（如"前两天"）→ 静默降级返回 None，原文保留
- time_base 显式传参不卡死（跨日连续调用结果跟随新 base）
- normalize_relative：命中相对时间归一化为绝对日期串
- parse_time_point：解析"下周三""9月15号"为 aware UTC
- verify_llm_date：校验 LLM 日期是否真能从原文推出（防幻觉）
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.conversation import time_normalizer

# 北京时间偏移
_TZ8 = timezone(timedelta(hours=8))
# 固定的“当前时间”：2026-09-02 00:00 UTC == 北京 08:00
_NOW_UTC = datetime(2026, 9, 2, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _reset_jionlp_cache():
    """每个用例重置 jionlp 惰性导入缓存，避免用例间污染。"""
    time_normalizer._JIONLP_AVAILABLE = None
    time_normalizer._jio = None
    yield
    time_normalizer._JIONLP_AVAILABLE = None
    time_normalizer._jio = None


class TestJionlpMissing:
    """jionlp 缺失（导入失败）→ 全链路降级。"""

    def test_extract_time_spans_returns_empty(self) -> None:
        with patch.object(time_normalizer, "_load_jionlp", return_value=False):
            assert time_normalizer.extract_time_spans("下周三", _NOW_UTC, _TZ8) == []

    def test_normalize_relative_returns_none(self) -> None:
        with patch.object(time_normalizer, "_load_jionlp", return_value=False):
            assert time_normalizer.normalize_relative("下周三", _NOW_UTC, _TZ8) is None

    def test_parse_time_point_returns_none(self) -> None:
        with patch.object(time_normalizer, "_load_jionlp", return_value=False):
            assert time_normalizer.parse_time_point("下周三", _NOW_UTC, _TZ8) is None

    def test_verify_llm_date_defaults_to_trust(self) -> None:
        """无校验能力时默认信任 LLM（不武断否决）。"""
        with patch.object(time_normalizer, "_load_jionlp", return_value=False):
            assert time_normalizer.verify_llm_date(
                "2026-09-09", "下周三开会", _NOW_UTC, _TZ8
            ) is True


class TestJionlpValueError:
    """jionlp 抛 ValueError（不支持的表达）→ 静默降级。"""

    def test_extract_time_spans_catches_value_error(self) -> None:
        mock_jio = MagicMock()
        mock_jio.ner.extract_time.side_effect = ValueError("不支持")
        with patch.object(time_normalizer, "_load_jionlp", return_value=True), \
                patch.object(time_normalizer, "_jio", mock_jio):
            assert time_normalizer.extract_time_spans("前两天", _NOW_UTC, _TZ8) == []

    def test_parse_time_point_catches_value_error(self) -> None:
        mock_jio = MagicMock()
        mock_jio.parse_time.side_effect = ValueError("不支持")
        with patch.object(time_normalizer, "_load_jionlp", return_value=True), \
                patch.object(time_normalizer, "_jio", mock_jio):
            assert time_normalizer.parse_time_point("前两天", _NOW_UTC, _TZ8) is None

    def test_unexpected_exception_logged_and_degraded(self) -> None:
        """非 ValueError 异常也静默降级，不传播到主链路。"""
        mock_jio = MagicMock()
        mock_jio.ner.extract_time.side_effect = RuntimeError("内部错误")
        with patch.object(time_normalizer, "_load_jionlp", return_value=True), \
                patch.object(time_normalizer, "_jio", mock_jio):
            assert time_normalizer.extract_time_spans("下周三", _NOW_UTC, _TZ8) == []


class TestTimeBaseNotStuck:
    """time_base 显式传参不卡死在初次启动日期。"""

    def test_time_base_follows_new_base(self) -> None:
        """连续两次调用跨越模拟日期变更 → 结果跟随新 base。"""
        mock_jio = MagicMock()
        # 模拟 jionlp 行为：time_base 不同则结果不同
        def _extract(text, time_base):
            return [{
                "text": text, "offset": [0, 2], "type": "time_point",
                "detail": {
                    "type": "time_point", "definition": "accurate",
                    "time": [time_base.strftime("%Y-%m-%d 00:00:00")],
                },
            }]
        mock_jio.ner.extract_time.side_effect = _extract
        with patch.object(time_normalizer, "_load_jionlp", return_value=True), \
                patch.object(time_normalizer, "_jio", mock_jio):
            base1 = datetime(2026, 9, 2, tzinfo=timezone.utc)
            base2 = datetime(2026, 9, 3, tzinfo=timezone.utc)
            r1 = time_normalizer.extract_time_spans("明天", base1, _TZ8)
            r2 = time_normalizer.extract_time_spans("明天", base2, _TZ8)
        assert r1 != r2
        assert "2026-09-02" in r1[0]["detail"]["time"][0]
        assert "2026-09-03" in r2[0]["detail"]["time"][0]


class TestNormalizeRelative:
    """相对时间归一化为绝对日期串。"""

    def test_next_wednesday(self) -> None:
        mock_jio = MagicMock()
        mock_jio.ner.extract_time.return_value = [{
            "text": "下周三", "offset": [0, 3], "type": "time_point",
            "detail": {
                "type": "time_point", "definition": "accurate",
                "time": ["2026-09-09 00:00:00", "2026-09-09 23:59:59"],
            },
        }]
        with patch.object(time_normalizer, "_load_jionlp", return_value=True), \
                patch.object(time_normalizer, "_jio", mock_jio):
            assert time_normalizer.normalize_relative("下周三", _NOW_UTC, _TZ8) == "2026-09-09"

    def test_blur_definition_returns_none(self) -> None:
        """模糊时间（definition=blur）不归一化，交 LLM 兜底。"""
        mock_jio = MagicMock()
        mock_jio.ner.extract_time.return_value = [{
            "text": "前两天", "offset": [0, 3], "type": "time_span",
            "detail": {
                "type": "time_span", "definition": "blur",
                "time": ["2026-08-26 00:00:00", "2026-08-31 23:59:59"],
            },
        }]
        with patch.object(time_normalizer, "_load_jionlp", return_value=True), \
                patch.object(time_normalizer, "_jio", mock_jio):
            assert time_normalizer.normalize_relative("前两天", _NOW_UTC, _TZ8) is None

    def test_multiple_spans_returns_none(self) -> None:
        """多个时间点不归一化，避免误改。"""
        mock_jio = MagicMock()
        mock_jio.ner.extract_time.return_value = [
            {"text": "下周三", "offset": [0, 3], "type": "time_point",
             "detail": {"type": "time_point", "definition": "accurate",
                        "time": ["2026-09-09 00:00:00", "2026-09-09 23:59:59"]}},
            {"text": "下周五", "offset": [4, 7], "type": "time_point",
             "detail": {"type": "time_point", "definition": "accurate",
                        "time": ["2026-09-11 00:00:00", "2026-09-11 23:59:59"]}},
        ]
        with patch.object(time_normalizer, "_load_jionlp", return_value=True), \
                patch.object(time_normalizer, "_jio", mock_jio):
            assert time_normalizer.normalize_relative("下周三和下周五", _NOW_UTC, _TZ8) is None

    def test_no_span_returns_none(self) -> None:
        mock_jio = MagicMock()
        mock_jio.ner.extract_time.return_value = []
        with patch.object(time_normalizer, "_load_jionlp", return_value=True), \
                patch.object(time_normalizer, "_jio", mock_jio):
            assert time_normalizer.normalize_relative("今天天气不错", _NOW_UTC, _TZ8) is None


class TestParseTimePoint:
    """parse_time 解析为 aware UTC 时间点。"""

    def test_parse_absolute_date(self) -> None:
        mock_jio = MagicMock()
        mock_jio.parse_time.return_value = {
            "type": "time_point", "definition": "accurate",
            "time": ["2026-09-10 00:00:00", "2026-09-10 23:59:59"],
        }
        with patch.object(time_normalizer, "_load_jionlp", return_value=True), \
                patch.object(time_normalizer, "_jio", mock_jio):
            result = time_normalizer.parse_time_point("9月10号", _NOW_UTC, _TZ8)
        assert result is not None
        # 北京 2026-09-10 00:00 = UTC 2026-09-09 16:00
        assert result == datetime(2026, 9, 9, 16, 0, tzinfo=timezone.utc)

    def test_parse_non_time_point_returns_none(self) -> None:
        """time_span/time_period 等非 point 类型返回 None。"""
        mock_jio = MagicMock()
        mock_jio.parse_time.return_value = {
            "type": "time_span", "definition": "blur",
            "time": ["2026-08-26 00:00:00", "2026-08-31 23:59:59"],
        }
        with patch.object(time_normalizer, "_load_jionlp", return_value=True), \
                patch.object(time_normalizer, "_jio", mock_jio):
            assert time_normalizer.parse_time_point("前两天", _NOW_UTC, _TZ8) is None


class TestVerifyLlmDate:
    """校验 LLM 日期是否真能从原文推出（防幻觉）。"""

    def test_matching_date_returns_true(self) -> None:
        mock_jio = MagicMock()
        mock_jio.ner.extract_time.return_value = [{
            "text": "下周三", "offset": [0, 3], "type": "time_point",
            "detail": {"type": "time_point", "definition": "accurate",
                       "time": ["2026-09-09 00:00:00", "2026-09-09 23:59:59"]},
        }]
        with patch.object(time_normalizer, "_load_jionlp", return_value=True), \
                patch.object(time_normalizer, "_jio", mock_jio):
            assert time_normalizer.verify_llm_date(
                "2026-09-09", "我下周三有面试", _NOW_UTC, _TZ8
            ) is True

    def test_mismatched_date_returns_false(self) -> None:
        """原文含时间实体但与 claim 不符 → 证伪。"""
        mock_jio = MagicMock()
        mock_jio.ner.extract_time.return_value = [{
            "text": "下周三", "offset": [0, 3], "type": "time_point",
            "detail": {"type": "time_point", "definition": "accurate",
                       "time": ["2026-09-09 00:00:00", "2026-09-09 23:59:59"]},
        }]
        with patch.object(time_normalizer, "_load_jionlp", return_value=True), \
                patch.object(time_normalizer, "_jio", mock_jio):
            assert time_normalizer.verify_llm_date(
                "2026-09-15", "我下周三有面试", _NOW_UTC, _TZ8
            ) is False

    def test_no_time_entity_returns_true(self) -> None:
        """原文无时间实体 → 无法证伪，默认信任。"""
        mock_jio = MagicMock()
        mock_jio.ner.extract_time.return_value = []
        with patch.object(time_normalizer, "_load_jionlp", return_value=True), \
                patch.object(time_normalizer, "_jio", mock_jio):
            assert time_normalizer.verify_llm_date(
                "2026-09-09", "我喜欢喝咖啡", _NOW_UTC, _TZ8
            ) is True

    def test_invalid_claim_returns_true(self) -> None:
        """claim 本身非法 → 由调用方按 None 处理，此处不证伪。"""
        mock_jio = MagicMock()
        with patch.object(time_normalizer, "_load_jionlp", return_value=True), \
                patch.object(time_normalizer, "_jio", mock_jio):
            assert time_normalizer.verify_llm_date(
                "not-a-date", "下周三开会", _NOW_UTC, _TZ8
            ) is True


class TestRealJionlp:
    """真实 jionlp 集成测试（依赖已安装时运行，否则跳过）。"""

    @pytest.fixture(autouse=True)
    def _require_jionlp(self) -> None:
        """jionlp 未安装时跳过本组测试。"""
        if not time_normalizer._load_jionlp():
            pytest.skip("jionlp 未安装")

    def test_real_extract_time(self) -> None:
        """真实 extract_time：下周三 → 2026-09-09。"""
        spans = time_normalizer.extract_time_spans("下周三下午三点开会", _NOW_UTC, _TZ8)
        assert len(spans) == 1
        assert spans[0]["detail"]["time"][0].startswith("2026-09-09")

    def test_real_normalize_relative(self) -> None:
        """真实 normalize_relative：下周三 → 2026-09-09。"""
        assert time_normalizer.normalize_relative("下周三", _NOW_UTC, _TZ8) == "2026-09-09"

    def test_real_parse_time_point(self) -> None:
        """真实 parse_time_point：9月15号 → 北京 2026-09-15 00:00 = UTC 09-14 16:00。"""
        result = time_normalizer.parse_time_point("9月15号", _NOW_UTC, _TZ8)
        assert result is not None
        assert result == datetime(2026, 9, 14, 16, 0, tzinfo=timezone.utc)

    def test_real_verify_llm_date(self) -> None:
        """真实 verify_llm_date：原文含"下周三" → claim 2026-09-09 可信。"""
        assert time_normalizer.verify_llm_date(
            "2026-09-09", "我下周三有面试", _NOW_UTC, _TZ8
        ) is True
        assert time_normalizer.verify_llm_date(
            "2026-09-15", "我下周三有面试", _NOW_UTC, _TZ8
        ) is False
