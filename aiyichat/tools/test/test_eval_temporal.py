"""钟点一致性尺子（`tools/eval/run_eval.py` 的 temporal 指标块）的离线自证。

全部离线：不调 LLM、不连 Mongo、不写 DB——本轮只造尺子并自证"尺子量得准"。

四条最值钱的断言：
1. `test_expectation_table_is_pinned_per_hour`：期待表逐点钉死（含跨午夜与 `days` 限定），
   且**开关关着时期待值不变**——`_warmth_expected` 踩过"期待跟着被测开关走"的坑，
   那样关档跑对照时"应命中"变成 0 例，两份结果没法比、整轮实验作废；
2. `test_unpinned_case_keeps_static_prefix_bytes`：未标注钟点的用例，静态前缀逐字节不变。
   易变文本一旦误写进 system 前缀就会击穿 prompt 前缀缓存（首字延迟从几秒恶化到几十秒），
   这类回归普通断言看不出来，必须直接比字节；
3. `test_dimension_list_is_unchanged`：`DIMENSIONS` 仍是原来那 5 维。把新维度塞进去会让
   所有旧基线 JSON 立刻没法对照，且 `_overall` 换了尺、裁判 rubric 也得跟着改；
4. `test_gsb_skips_old_baselines_without_temporal`：旧基线没有 `temporal` 这个 key 时，
   GSB 必须守卫跳过而不是 KeyError，也不能把"没测过"当成"没命中"。
"""

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.config import settings
from app.llm.prompt_assembler import _day_phase
from app.models.profile import ProfileLifeRoutine, ProfileLifeSegment
from tools.eval import run_eval as R
from tools.test.time_replay import frozen_clock

# 未标注钟点的最小用例（结构与内置用例一致，离线可跑）
_CASE = {
    "name": "离线自证-最小用例",
    "persona": "你是温柔体贴的林诗雨，说话亲切。",
    "memory": [],
    "history": [{"role": "user", "content": "在吗"}],
    "user_input": "在吗",
}

# 真实文案取样（与生产渲染逐字一致，用来单独钉住抽取式）：
# prompt_assembler._render_context_hint 的【当前时刻】行
_MSG_TEXT = "【当前时刻】现在是 10月19日（星期一）09:05，上午。"
# life_routine.render_routine_hint 的【你的作息】行（含 _ROUTINE_GUARD 护栏全文）
_ROUTINE_TEXT = (
    "【你的作息】此刻（09:05，星期一，你所在时区）是你在「上课」——在教室不敢看手机。"
    "只据此决定你在做什么、回得快慢；不向对方报备行程，也绝不因此不回消息或拖延回复。"
)


def _anchor(hour: int, weekday: int = 0) -> datetime:
    """按 (UTC 钟点, 星期) 取锚点时刻（分钟固定 05，不落在时段边界上）。"""
    return R._case_anchor({"name": "anchor", "at_utc_hour": hour, "weekday": weekday})


class TestExpectationTable:
    """期待表逐点钉死：(UTC 钟点, 星期, 期望 activity)，0=周一。"""

    # 作息表：上课 8~12（每天）/ 上班 14~18（每天）/ 就寝 22~6（跨午夜）/
    # 跑代码 9~17（days=[5] 即只周六）。顺序即优先级，先具体后笼统。
    @pytest.mark.parametrize(
        "hour,weekday,activity",
        [
            (0, 0, "就寝"),      # 跨午夜段的后半（22~6）
            (5, 0, "就寝"),      # 跨午夜段内，end=6 不含 → 5 仍命中
            (6, 0, ""),          # end 不含：整点翻到"不命中"
            (7, 0, ""),          # 空档
            (8, 0, "上课"),      # start 含
            (11, 0, "上课"),
            (12, 0, ""),         # 空档（周一时 days 段不命中）
            (12, 5, "跑代码"),   # days=[5]：同一钟点落在周六才命中
            (13, 0, ""),
            (14, 0, "上班"),
            (17, 0, "上班"),     # 内置用例「复读压力-重复提问」的取样点
            (18, 0, ""),         # end 不含
            (21, 0, ""),         # 内置用例「情感支持-孤单」类钟点：不该有作息
            (22, 0, "就寝"),     # 跨午夜段起点（内置用例「隐含情绪-加班」）
            (23, 0, "就寝"),
            (10, 5, "上课"),     # 周六 10 点：两段都命中，取第一条（先具体后笼统）
        ],
    )
    def test_activity_per_hour(self, hour: int, weekday: int, activity: str) -> None:
        exp = R._temporal_expected(R._temporal_routine(), _anchor(hour, weekday))
        assert exp["expected_activity"] == activity
        assert exp["expected"] is bool(activity)
        # 作息时区在尺子里钉为 UTC，所以期望的本地时刻恰好等于声明钟点
        assert exp["routine_hhmm"] == f"{hour:02d}:05"

    def test_switch_off_does_not_move_expectations(self) -> None:
        """期待值与被测开关无关：开关关着时期望表必须一字不改。"""
        hours = list(range(24))
        with patch.object(settings, "profile_routine_enabled", False):
            off = [
                R._temporal_expected(R._temporal_routine(), _anchor(h)) for h in hours
            ]
        with patch.object(settings, "profile_routine_enabled", True):
            on = [
                R._temporal_expected(R._temporal_routine(), _anchor(h)) for h in hours
            ]
        assert off == on
        # 命中小时数钉死：上课 8~11 (4) + 上班 14~17 (4) + 就寝 22~23/0~5 (8)
        assert sum(1 for e in off if e["expected"]) == 16

    def test_custom_routine_timezone_is_honoured(self) -> None:
        """期待值走生产 `resolve_tz`：非 UTC 作息时区也要按角色本地时判定。

        尺子内置表把时区钉为 UTC（已知局限，结论不外推），这里用一张 Asia/Shanghai 的
        表单独钉住"期望计算确实按作息时区换算"，免得那条路径从没被量过。
        """
        routine = ProfileLifeRoutine(
            timezone="Asia/Shanghai",
            segments=[ProfileLifeSegment(start_hour=9, end_hour=10, activity="早会")],
        )
        # 01:05 UTC = 09:05 上海 → 命中；02:05 UTC = 10:05 上海 → end 不含，不命中
        assert R._temporal_expected(routine, _anchor(1))["expected_activity"] == "早会"
        assert R._temporal_expected(routine, _anchor(2))["expected_activity"] == ""


class TestInjectionFlip:
    """冻结 09:05 → 14:05 时作支行里的 activity 必须翻转（仿 test_life_routine 的断法）。"""

    def test_activity_flips_when_clock_advances(self) -> None:
        case9 = {**_CASE, "at_utc_hour": 9, "weekday": 0}
        case14 = {**_CASE, "at_utc_hour": 14, "weekday": 0}
        modules = ("app.llm.prompt_assembler", "app.conversation.life_routine")
        with patch.object(settings, "profile_routine_enabled", True), frozen_clock(
            _anchor(9), modules
        ) as clock:
            first = R._build_messages(case9)
            clock.advance(hours=5)
            second = R._build_messages(case14)

        tail_first = R._temporal_tail(first)
        tail_second = R._temporal_tail(second)
        assert "【你的作息】" in tail_first and "【你的作息】" in tail_second
        assert "上课" in tail_first and "上班" in tail_second
        assert first[0]["content"] == second[0]["content"], "作息误进了 system 前缀"

    def test_injection_only_in_dynamic_tail(self) -> None:
        """注入位置：只进动态尾段，messages[0]（静态前缀）里一个字都不许有。"""
        case = {**_CASE, "at_utc_hour": 15}
        with patch.object(settings, "profile_routine_enabled", True):
            messages = R._build_messages(case)
        assert messages[0]["role"] == "system"
        assert "【你的作息】" not in messages[0]["content"]
        assert messages[-1]["role"] == "user"
        temporal = R._temporal_metrics(case, messages, "", _anchor(15))
        assert temporal["injected"] is True
        assert temporal["injected_activity"] == "上班"
        assert temporal["guard_present"] is True  # 护栏文案（不报备行程）必须在
        assert temporal["hit"] is True


class TestExtraction:
    """抽取式对真实文案的解析（正则与生产渲染文本必须逐字吻合）。"""

    def test_message_clock_text(self) -> None:
        match = R._MESSAGE_CLOCK_RE.search(_MSG_TEXT)
        assert match is not None, "抽取式与【当前时刻】真实文案不吻合"
        assert (match.group(1), match.group(2), match.group(3)) == ("10", "19", "星期一")
        assert R._hhmm(match.group(4), match.group(5)) == "09:05"
        assert match.group(6) == "上午"

    def test_routine_clock_and_activity_text(self) -> None:
        clock_match = R._ROUTINE_CLOCK_RE.search(_ROUTINE_TEXT)
        assert clock_match is not None, "抽取式与【你的作息】真实文案不吻合"
        assert R._hhmm(clock_match.group(1), clock_match.group(2)) == "09:05"
        assert clock_match.group(3) == "星期一"
        activity_match = R._ROUTINE_ACTIVITY_RE.search(_ROUTINE_TEXT)
        assert activity_match is not None
        assert activity_match.group(1) == "上课"

    def test_activity_extracted_when_phrase_is_blank(self) -> None:
        """phrase 留空时生产渲染成「activity」而没有破折号后半，抽取式必须照样吃到。"""
        routine = ProfileLifeRoutine(
            timezone="UTC",
            segments=[ProfileLifeSegment(start_hour=9, end_hour=10, activity="值班")],
        )
        case = {**_CASE, "at_utc_hour": 9}
        with patch.object(settings, "profile_routine_enabled", True), patch.object(
            R, "_temporal_routine", lambda: routine
        ):
            messages = R._build_messages(case)
            temporal = R._temporal_metrics(case, messages, "", _anchor(9))
        assert temporal["injected_activity"] == "值班"
        assert temporal["activity_ok"] is True

    def test_extraction_against_real_assembled_product(self) -> None:
        """直接拿真实 assemble 产物对照：两个时区各判各的，混比就会假失败。"""
        case = {**_CASE, "at_utc_hour": 9, "weekday": 0}
        with patch.object(settings, "default_timezone_offset", 8), patch.object(
            settings, "profile_routine_enabled", True
        ):
            anchor = _anchor(9)
            messages = R._build_messages(case, anchor)
            temporal = R._temporal_metrics(case, messages, "", anchor)
        # 服务时区 UTC+8 → 【当前时刻】是 17:05；作息时区 UTC → 作支行是 09:05
        assert temporal["message_clock"] == "17:05" == temporal["message_clock_expected"]
        assert temporal["message_clock_ok"] is True and temporal["message_date_ok"] is True
        assert temporal["routine_clock"] == "09:05" == temporal["routine_clock_expected"]
        assert temporal["routine_clock_ok"] is True
        assert temporal["phase"] == _day_phase(17) == "下午"
        assert temporal["clock_consistent"] is True

    def test_missing_message_clock_is_unavailable_not_failed(self) -> None:
        """时间感知总开关关着 → 没有【当前时刻】行：记"该检查不可用"而不是判失败。"""
        case = {**_CASE, "at_utc_hour": 9}
        with patch.object(settings, "message_time_enabled", False), patch.object(
            settings, "profile_routine_enabled", True
        ):
            anchor = _anchor(9)
            messages = R._build_messages(case, anchor)
            temporal = R._temporal_metrics(case, messages, "", anchor)
        assert temporal["message_time_available"] is False
        assert temporal["message_clock_ok"] is None
        assert temporal["message_date_ok"] is None
        assert temporal["injected"] is True
        assert temporal["hit"] is True  # 不可用不能被算成不命中


class TestContradictionWords:
    """反例词表：只在对应钟点档算矛盾，宁漏不误。"""

    def test_same_sentence_only_contradicts_in_its_bucket(self) -> None:
        text = "早上好 外面太阳真好"
        assert R._temporal_contradictions("凌晨", text) == ["早上好"]
        # 上午说"早上好"合法：同一句话换档就不能命中
        assert R._temporal_contradictions("上午", text) == []
        assert R._temporal_contradictions("", text) == []
        assert R._temporal_contradictions("凌晨", "") == []

    def test_buckets_cover_day_phase_and_stay_small(self) -> None:
        """分档口径必须与生产 `_day_phase` 一致；每档最多 3 个强标记词（红线）。"""
        phases = {_day_phase(hour) for hour in range(24)}
        assert set(R._TEMPORAL_CONTRADICTION_WORDS) == phases
        assert all(len(words) <= 3 for words in R._TEMPORAL_CONTRADICTION_WORDS.values())

    def test_contradiction_recorded_on_cleaned_text(self) -> None:
        """矛盾扫的是清洗后的正文（与 ai_tells/长度同一口径）；内部标记不该被误算。"""
        case = {**_CASE, "at_utc_hour": 17, "weekday": 0}
        with patch.object(settings, "default_timezone_offset", 8), patch.object(
            settings, "profile_routine_enabled", True
        ):
            anchor = _anchor(17)
            messages = R._build_messages(case, anchor)
            # 17:05 UTC → 服务侧 01:05（凌晨档）；作息侧仍是「上班」
            temporal = R._temporal_metrics(case, messages, "早上好呀 [IMG:晨光]", anchor)
        assert temporal["phase"] == "凌晨"
        assert temporal["expected_activity"] == "上班"
        assert temporal["contradictions"] == ["早上好"]
        assert temporal["contradiction"] is True


class TestBehaviourSelfProof:
    """行为自证闸：新尺子不得改变既有评测行为。"""

    def test_profile_identical_with_and_without_hour(self) -> None:
        assert R._build_profile(dict(_CASE)) == R._build_profile(
            {**_CASE, "at_utc_hour": 9, "weekday": 3}
        )

    def test_unpinned_output_carries_no_eval_strings(self) -> None:
        """未标注钟点的用例：产物里不得出现任何评测专用串。"""
        messages = R._build_messages(dict(_CASE))
        blob = json.dumps(messages, ensure_ascii=False)
        for marker in ("temporal", "at_utc_hour", "weekday", "锚点", "跑代码"):
            assert marker not in blob
        # 开关关着（生产默认）时作息表本就什么都不会注入
        assert "【你的作息】" not in blob

    def test_unpinned_case_keeps_static_prefix_bytes(self) -> None:
        """同一未标注用例，在不同钟点重组：静态前缀逐字节相同，只有动态尾段变。

        这条是项目硬红线：易变文本误写进 system 前缀会击穿 prompt 前缀缓存，
        首字延迟从几秒恶化到几十秒。
        """
        modules = ("app.llm.prompt_assembler", "app.conversation.life_routine")
        with patch.object(settings, "profile_routine_enabled", True), frozen_clock(
            _anchor(9), modules
        ) as clock:
            first = R._build_messages(dict(_CASE))
            clock.advance(hours=5)
            second = R._build_messages(dict(_CASE))
        assert first[0]["content"] == second[0]["content"]
        assert R._temporal_tail(first) != R._temporal_tail(second)

    def test_unpinned_anchor_is_real_clock(self) -> None:
        anchor = R._case_anchor(dict(_CASE))
        assert anchor.tzinfo is not None
        assert abs((datetime.now(timezone.utc) - anchor).total_seconds()) < 60
        assert R._case_anchor({**_CASE, "at_utc_hour": 9}) == datetime(
            2026, 10, 19, 9, 5, tzinfo=timezone.utc
        )
        assert R._case_anchor({**_CASE, "at_utc_hour": 9}).weekday() == 0

    @pytest.mark.parametrize(
        "field",
        [{"at_utc_hour": 24}, {"at_utc_hour": -1}, {"at_utc_hour": 9, "weekday": 7}],
    )
    def test_rejects_out_of_range_clock(self, field: dict) -> None:
        with pytest.raises(ValueError):
            R._case_anchor({**_CASE, **field})

    def test_dimension_list_is_unchanged(self) -> None:
        """生产行为自检：`DIMENSIONS` 必须还是原来那 5 维。

        把 temporal 加进去会让所有旧基线 JSON 立刻没法对照（`:430` 要求每例
        五个维度齐全、`_overall` 为 None 的例被 GSB 整批跳过），且 `_overall`
        变成 6 维均值就是换了尺；裁判 rubric 同理不能动。
        """
        assert R.DIMENSIONS == [
            "persona_consistency",
            "empathy",
            "memory_accuracy",
            "naturalness",
            "boundary_honesty",
        ]
        assert "temporal" not in R.DIMENSIONS
        assert "temporal" not in R._JUDGE_SYSTEM_PROMPT


class TestReporting:
    """汇总打印与 GSB 守卫。"""

    def _record(self, name: str, **overrides: object) -> dict:
        temporal = {
            "pinned": True,
            "expected": True,
            "injected": True,
            "missing": False,
            "extra": False,
            "activity_ok": True,
            "guard_present": True,
            "expected_activity": "上课",
            "injected_activity": "上课",
            "message_time_available": True,
            "message_clock_ok": True,
            "message_clock_expected": "17:05",
            "message_clock": "17:05",
            "routine_clock_ok": True,
            "routine_clock": "09:05",
            "routine_clock_expected": "09:05",
            "contradictions": [],
            "phase": "下午",
            "anchor_utc": "2026-10-19 09:05:00",
            "at_utc_hour": 9,
            "weekday": 0,
            "hit": True,
        }
        temporal.update(overrides)
        return {
            "name": name,
            "scores": {d: 4 for d in R.DIMENSIONS},
            "response": "好呀 我知道啦",
            "temporal": temporal,
        }

    def test_summary_counts_expected_and_injected(self, capsys) -> None:
        R._print_temporal_summary(
            [
                self._record("a"),
                self._record("b", expected=False, injected=False, hit=True),
                self._record("c", injected=False, hit=False, missing=True),
                self._record("d", contradictions=["早上好"]),
            ]
        )
        out = capsys.readouterr().out
        assert "应命中 3/4 例，实际注入 2/4 例（漏注入 1、多注入 0）" in out
        assert "钟点矛盾用例: 1/4" in out
        assert "漏注入: c" in out
        assert "钟点矛盾: d" in out

    def test_gsb_guard_skips_baselines_without_temporal(self, capsys) -> None:
        scores = {d: 4 for d in R.DIMENSIONS}
        del scores
        current = [self._record("a"), self._record("b")]
        # 旧基线：没有 temporal 这个 key（尺子是本轮才造的）
        baseline = [{k: v for k, v in r.items() if k != "temporal"} for r in current]
        R._gsb(current, baseline)  # 基线没有 temporal key：不得 KeyError
        out = capsys.readouterr().out
        assert "应命中用例的钟点一致性命中率" not in out

    def test_case_line_prints_without_crash(self, capsys) -> None:
        """逐例打印路径（dry-run 也走它）：钉了钟点的用例要把锚点报出来。"""
        case = {**_CASE, "at_utc_hour": 9, "weekday": 0}
        with patch.object(settings, "profile_routine_enabled", True):
            anchor = R._case_anchor(case)
            messages = R._build_messages(case, anchor)
            temporal = R._temporal_metrics(case, messages, "好呀", anchor)
        R._print_case_temporal(temporal)
        out = capsys.readouterr().out
        assert "锚点 2026-10-19 09:05:00" in out
        assert "应命中=True(上课)" in out and "实际注入=True(上课)" in out

    def test_gsb_prints_temporal_rows_when_both_have_it(self, capsys) -> None:
        current = [self._record("a")]
        old = self._record("a", hit=False, injected=False, missing=True)
        baseline = [old]
        R._gsb(current, baseline)
        out = capsys.readouterr().out
        assert "钟点 0 → 1" in out
        assert "应命中用例的钟点一致性命中率: 0/1 → 1/1" in out
