"""角色作息（C1）测试：纯状态机 + 提示词注入位置 + 前缀缓存不被击穿。

最有价值的一条是 `TestPrefixCacheSafe`：作息文本每小时都变，
一旦误进 system 前缀就会击穿 prompt 前缀缓存（实测首字延迟从几秒恶化到几十秒），
而这类回归普通断言看不出来，必须直接比对前缀字节。
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.admin.routes import features  # noqa: F401  （确保开关清单模块已加载）
from app.config import settings
from app.conversation.life_routine import current_segment, render_routine_hint
from app.llm.prompt_assembler import PromptAssembler
from app.models.profile import (
    ProfileLifeRoutine,
    ProfileLifeSegment,
    PromptProfileInDB,
)
from app.profiles.service import _parse_life_routine
from tools.test.time_replay import frozen_clock

MONDAY_1420 = datetime(2026, 10, 19, 14, 20, tzinfo=timezone.utc)


def _routine(*segments: ProfileLifeSegment, timezone_name: str = "UTC") -> ProfileLifeRoutine:
    return ProfileLifeRoutine(timezone=timezone_name, segments=list(segments))


class TestSegmentMatching:
    """时段命中的三种边界：普通区间、跨午夜、工作日限定。"""

    def test_plain_range_hits_end_exclusive(self) -> None:
        routine = _routine(ProfileLifeSegment(start_hour=8, end_hour=12, activity="上班"))
        assert current_segment(routine, MONDAY_1420) is None
        assert current_segment(routine, MONDAY_1420.replace(hour=8)).activity == "上班"  # type: ignore[union-attr]
        assert current_segment(routine, MONDAY_1420.replace(hour=11, minute=59)) is not None
        # 止时刻整点不含
        assert current_segment(routine, MONDAY_1420.replace(hour=12)) is None

    def test_cross_midnight_range(self) -> None:
        routine = _routine(ProfileLifeSegment(start_hour=22, end_hour=6, activity="就寝"))
        assert current_segment(routine, MONDAY_1420.replace(hour=23)) is not None
        assert current_segment(routine, MONDAY_1420.replace(hour=3)) is not None
        assert current_segment(routine, MONDAY_1420.replace(hour=9)) is None

    def test_days_filter_and_empty_means_every_day(self) -> None:
        routine = _routine(
            ProfileLifeSegment(start_hour=8, end_hour=16, activity="上课", days=[0, 2, 4])
        )
        assert current_segment(routine, MONDAY_1420) is not None  # 周一
        assert current_segment(routine, MONDAY_1420 + timedelta(days=1)) is None  # 周二
        empty_days = _routine(ProfileLifeSegment(start_hour=8, end_hour=16, activity="上班"))
        assert current_segment(empty_days, MONDAY_1420 + timedelta(days=3)) is not None

    def test_first_match_wins_so_exceptions_go_first(self) -> None:
        routine = _routine(
            ProfileLifeSegment(start_hour=14, end_hour=15, activity="在医务室"),
            ProfileLifeSegment(start_hour=8, end_hour=18, activity="上班"),
        )
        assert current_segment(routine, MONDAY_1420).activity == "在医务室"  # type: ignore[union-attr]

    def test_start_equals_end_is_never_hit(self) -> None:
        """start == end 是运营填错的行，不能变成"全天命中"。"""
        routine = _routine(ProfileLifeSegment(start_hour=9, end_hour=9, activity="填错"))
        assert current_segment(routine, MONDAY_1420.replace(hour=9)) is None


class TestHintWording:
    """注入文案必须自带"不回消息/拖延"的硬护栏（用户两次否决过那个方向）。"""

    def test_hint_contains_guard(self) -> None:
        routine = _routine(
            ProfileLifeSegment(start_hour=14, end_hour=15, activity="上课", phrase="刚下课")
        )
        hint = render_routine_hint(routine, MONDAY_1420)
        assert "【你的作息】" in hint
        assert "上课" in hint and "刚下课" in hint
        assert "不回消息" in hint and "拖延回复" in hint

    def test_empty_routine_or_activity_renders_nothing(self) -> None:
        assert render_routine_hint(_routine(), MONDAY_1420) == ""
        blank = _routine(ProfileLifeSegment(start_hour=14, end_hour=15, activity="  "))
        assert render_routine_hint(blank, MONDAY_1420) == ""


def _profile(routine: ProfileLifeRoutine) -> PromptProfileInDB:
    return PromptProfileInDB(
        id="p-1", slug="companion", name="小依", life_routine=routine
    )


class TestInjectionPlacement:
    """作息文本只许出现在动态尾段（最后一条 system 消息），不得进静态前缀。"""

    def _assemble(self, profile: PromptProfileInDB):
        return PromptAssembler().assemble(
            profile=profile, history=[], identity_input="在吗", emotion_hint=None
        )

    def test_switch_off_injects_nothing(self) -> None:
        profile = _profile(
            _routine(ProfileLifeSegment(start_hour=8, end_hour=18, activity="上班"))
        )
        with patch.object(settings, "profile_routine_enabled", False):
            messages = self._assemble(profile)
        assert all("【你的作息】" not in m["content"] for m in messages)

    def test_switch_on_injects_into_tail_only(self) -> None:
        profile = _profile(
            _routine(ProfileLifeSegment(start_hour=8, end_hour=18, activity="上班"))
        )
        # 必须冻钟：_render_routine_hint 读的是真实墙钟，不冻则该用例只在
        # UTC 08:00-18:00 之间通过（CI 跑在 18:07 即整批红，本地白天永远绿）
        with patch.object(settings, "profile_routine_enabled", True), \
             frozen_clock(MONDAY_1420, modules=("app.llm.prompt_assembler",)):
            messages = self._assemble(profile)
        assert messages[0]["role"] == "system"
        assert "【你的作息】" not in messages[0]["content"]
        tail = [m for m in messages if m["role"] == "system" and "【你的作息】" in m["content"]]
        assert tail, "作息应注入到动态尾段的 system 消息里"
        # 动态尾段必须紧贴对方输入之前（前缀缓存的最后一道边界）
        assert messages[-1]["role"] == "user"

    def test_legacy_profile_without_routine_is_unchanged(self) -> None:
        """老角色没这个字段：开着开关也必须逐字不变。"""
        profile = PromptProfileInDB(id="p-2", slug="old", name="老角色")
        with patch.object(settings, "profile_routine_enabled", True):
            messages = self._assemble(profile)
        assert all("【你的作息】" not in m["content"] for m in messages)


class TestPrefixCacheSafe:
    """跨越一小时后重新组装：静态前缀必须逐字相同，只有动态尾段变。"""

    def test_prefix_bytes_identical_across_hours(self) -> None:
        profile = _profile(
            _routine(
                ProfileLifeSegment(start_hour=8, end_hour=12, activity="上课"),
                ProfileLifeSegment(start_hour=14, end_hour=18, activity="上班"),
            )
        )
        modules = ("app.llm.prompt_assembler", "app.conversation.life_routine")
        with patch.object(settings, "profile_routine_enabled", True), frozen_clock(
            datetime(2026, 10, 19, 9, 5, tzinfo=timezone.utc), modules
        ) as clock:
            first = PromptAssembler().assemble(
                profile=profile, history=[], identity_input="在吗"
            )
            clock.advance(hours=5)
            second = PromptAssembler().assemble(
                profile=profile, history=[], identity_input="在吗"
            )

        assert first[0]["content"] == second[0]["content"], "作息误进了 system 前缀"
        hint_first = [m["content"] for m in first if "【你的作息】" in m["content"]]
        hint_second = [m["content"] for m in second if "【你的作息】" in m["content"]]
        assert hint_first and hint_second and hint_first != hint_second
        assert "上课" in hint_first[0] and "上班" in hint_second[0]


class _FakeForm:
    """只实现后台表单用到的两个方法，避免为此拉起 starlette。"""

    def __init__(self, rows: dict[str, list[str]], scalars: dict[str, str]) -> None:
        self._rows = rows
        self._scalars = scalars

    def getlist(self, key: str) -> list[str]:
        return self._rows.get(key, [])

    def get(self, key: str, default: str = "") -> str:
        return self._scalars.get(key, default)


class TestFormParsing:
    """一行填错不该清掉整个作息。"""

    def test_skips_blank_and_invalid_rows(self) -> None:
        form = _FakeForm(
            {
                "routine_start": ["8", "", "abc", "30", "14"],
                "routine_end": ["12", "18", "18", "18", "18"],
                "routine_activity": ["上课", "", "摸鱼", "填错小时", "上班"],
                "routine_phrase": ["早读困", "", "", "", "午后最容易出错"],
            },
            {"routine_timezone": "Asia/Shanghai"},
        )
        routine = _parse_life_routine(form)
        assert routine.timezone == "Asia/Shanghai"
        assert [s.activity for s in routine.segments] == ["上课", "上班"]
        assert routine.segments[1].phrase == "午后最容易出错"

    def test_empty_form_yields_empty_routine(self) -> None:
        routine = _parse_life_routine(_FakeForm({}, {}))
        assert routine.segments == [] and routine.timezone == ""
