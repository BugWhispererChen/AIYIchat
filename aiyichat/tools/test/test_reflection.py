"""反思层单测：触发节流、grounding 硬校验、注入渲染、洞见存取。

不触真库、不调模型（provider 与 settings 按需打桩）。
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from app.config import settings
from app.conversation import reflection
from app.conversation.memory_hygiene import new_fact_id
from app.conversation.reflection import (
    build_evidence_block,
    render_insight_lines,
    should_reflect,
    validate_insight,
)
from app.models.service_memory import MemoryFact, MemoryInsight, ServiceMemoryInDB

_NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def reflection_on():
    """钉住时间感知类开关，避免全量顺序里被其它用例改档造成假绿/假红。"""
    with mock.patch.object(settings, "memory_reflection_enabled", True), \
         mock.patch.object(settings, "reflection_min_facts", 12), \
         mock.patch.object(settings, "reflection_min_interval_hours", 24.0), \
         mock.patch.object(settings, "reflection_min_evidence", 2), \
         mock.patch.object(settings, "reflection_max_insights", 5):
        yield


def _facts(n: int) -> list[MemoryFact]:
    return [
        MemoryFact(fid=new_fact_id(), content=f"对方事实{i}") for i in range(n)
    ]


class TestShouldReflect:
    def test_off_means_never(self) -> None:
        with mock.patch.object(settings, "memory_reflection_enabled", False):
            assert should_reflect(99, None, _NOW) is False

    def test_not_enough_material(self, reflection_on: None) -> None:
        # 材料不足时归纳出来的只会是复述，因此宁可不做
        assert should_reflect(settings.reflection_min_facts - 1, None, _NOW) is False

    def test_first_time_when_enough(self, reflection_on: None) -> None:
        assert should_reflect(settings.reflection_min_facts, None, _NOW) is True

    def test_throttled_by_interval(self, reflection_on: None) -> None:
        recent = _NOW - timedelta(hours=settings.reflection_min_interval_hours - 1)
        assert should_reflect(50, recent, _NOW) is False
        older = _NOW - timedelta(hours=settings.reflection_min_interval_hours + 1)
        assert should_reflect(50, older, _NOW) is True

    def test_naive_timestamp_tolerated(self, reflection_on: None) -> None:
        # 存量数据可能不带时区，不应因相减报错而整轮反思失败
        naive = (_NOW - timedelta(days=2)).replace(tzinfo=None)
        assert should_reflect(50, naive, _NOW) is True


class TestGrounding:
    """来源不足或引用不存在编号的洞见必须整条丢弃。"""

    def test_enough_evidence_accepted(self, reflection_on: None) -> None:
        facts = _facts(3)
        text, index_to_fid = build_evidence_block(facts)
        assert "1. 对方事实0" in text
        insight = validate_insight(
            {"content": "对方这几天一直在加班", "kind": "trend", "evidence": [1, 2]},
            index_to_fid,
            2,
        )
        assert insight is not None
        assert insight.source_fids == [facts[0].fid, facts[1].fid]
        assert insight.fid  # 落库即可被后台按 fid 撤销

    def test_single_source_rejected(self, reflection_on: None) -> None:
        _, index_to_fid = build_evidence_block(_facts(3))
        assert validate_insight(
            {"content": "对方喜欢猫", "evidence": [1]}, index_to_fid, 2
        ) is None

    def test_fabricated_index_rejected(self, reflection_on: None) -> None:
        _, index_to_fid = build_evidence_block(_facts(3))
        # 编号 99 不存在：模型编造来源时不能被放行
        assert validate_insight(
            {"content": "对方要去旅行", "evidence": [1, 99, "x"]}, index_to_fid, 2
        ) is None

    def test_bad_payload_shapes_ignored(self, reflection_on: None) -> None:
        _, index_to_fid = build_evidence_block(_facts(3))
        for bad in (None, "文本", {}, {"content": "", "evidence": [1, 2]},
                    {"content": "有内容", "evidence": "1,2"}):
            assert validate_insight(bad, index_to_fid, 2) is None

    def test_unknown_kind_degrades_to_trend(self, reflection_on: None) -> None:
        _, index_to_fid = build_evidence_block(_facts(3))
        insight = validate_insight(
            {"content": "对方与我关系亲密", "kind": "玄学", "evidence": [1, 2]},
            index_to_fid, 2,
        )
        assert insight is not None and insight.kind == "trend"

    def test_invalidated_facts_not_offered(self) -> None:
        # 已失效的事实不能进反思素材，否则会把它们"复活"成持续判断
        live = MemoryFact(fid=new_fact_id(), content="对方在读研")
        dead = MemoryFact(
            fid=new_fact_id(),
            content="对方8月26日有面试",
            invalid_at=_NOW - timedelta(days=1),
        )
        text, index_to_fid = build_evidence_block([live, dead])
        assert "面试" not in text
        assert index_to_fid == {1: live.fid}


class TestRenderInsightLines:
    def _doc(self, insights: list[MemoryInsight]) -> ServiceMemoryInDB:
        return ServiceMemoryInDB(
            id="d1", service_id="s1", facts=[], insights=insights
        )

    def test_off_returns_empty(self) -> None:
        doc = self._doc([MemoryInsight(fid="a", content="对方一直在加班")])
        with mock.patch.object(settings, "memory_reflection_enabled", False):
            assert render_insight_lines(doc) == []

    def test_invalidated_excluded_and_capped(self, reflection_on: None) -> None:
        doc = self._doc(
            [MemoryInsight(fid="a", content="甲")]
            + [MemoryInsight(fid="b", content="乙", invalidated=True)]
            + [MemoryInsight(fid=str(i), content=f"丙{i}") for i in range(9)]
        )
        lines = render_insight_lines(doc)
        assert "乙" not in lines
        assert len(lines) == settings.reflection_max_insights

    def test_no_service_memory_is_safe(self, reflection_on: None) -> None:
        assert render_insight_lines(None) == []


class TestInjection:
    """洞见渲染进常驻稳定段，且不得带时间戳（前缀缓存硬约束）。"""

    def test_rendered_as_own_block(self, reflection_on: None) -> None:
        from app.llm.prompt_assembler import PromptAssembler

        text = PromptAssembler._render_memory_section(
            "", ["对方喜欢喝咖啡"], insight_lines=["对方这一个月一直在加班"]
        )
        assert "【你慢慢了解到的】" in text
        assert "- 对方这一个月一直在加班" in text
        assert "与上面逐条记录冲突时以记录为准" in text

    def test_absent_when_no_insights(self) -> None:
        from app.llm.prompt_assembler import PromptAssembler

        text = PromptAssembler._render_memory_section("", ["对方喜欢喝咖啡"])
        assert "【你慢慢了解到的】" not in text

    def test_no_clock_text_in_prefix(self, reflection_on: None) -> None:
        # 前缀段内出现"天前/于"这类逐轮变化文本会击穿 prompt 前缀缓存
        from app.llm.prompt_assembler import PromptAssembler

        text = PromptAssembler._render_memory_section(
            "", [], insight_lines=["对方最近睡得晚"]
        )
        block = text.split("【你慢慢了解到的】")[1]
        assert "天前" not in block and "记录于" not in block


class TestGenerateInsightsWiring:
    """generate_insights 必须把校验前的原始条目按上限截断，且异常不影响主链路。"""

    async def _call_failing_llm(self) -> list[MemoryInsight]:
        class _Boom:
            async def complete(self, **kwargs: object) -> str:
                raise RuntimeError("额度耗尽")

        with mock.patch.object(
            reflection, "get_llm_provider", return_value=_Boom()
        ):
            return await reflection.generate_insights(_facts(12))

    def test_llm_failure_yields_no_insights(self, reflection_on: None) -> None:
        # 反思是后台增强项：模型不可用时必须静默跳过，绝不把异常冒到对话主链路
        assert asyncio.run(self._call_failing_llm()) == []

    async def _parse_payload(self, facts: list[MemoryFact]) -> list[MemoryInsight]:
        _, index_to_fid = build_evidence_block(facts)
        payload = (
            '```json\n{"insights": [{"content": "对方连续几周都在加班",'
            ' "kind": "trend", "evidence": [1, 2]}]}\n```'
        )

        class _Ok:
            async def complete(self, **kwargs: object) -> str:
                return payload

        with mock.patch.object(reflection, "get_llm_provider", return_value=_Ok()):
            return await reflection.generate_insights(facts)

    def test_json_wrapped_in_code_fence_parsed(self, reflection_on: None) -> None:
        # 模型常把 JSON 包在代码块里，解析必须与抽取通道同一套容错
        facts = _facts(12)
        insights = asyncio.run(self._parse_payload(facts))
        assert len(insights) == 1
        assert insights[0].source_fids == [facts[0].fid, facts[1].fid]
        assert build_evidence_block(facts)[1][1] == facts[0].fid
