"""PromptAssembler 单元测试。

覆盖两大块：
1. 基础组装（系统提示词/历史/摘要/情绪注入）
2. 对话时间感知（日期锚点、时刻与间隔语义、时间标签、跨天分隔符、记忆时间后缀、审计字段）
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.models.profile import (
    PromptProfileInDB,
    ProfilePersona,
    ProfileLanguageStyle,
    ProfilePersonality,
    ProfileDialogueStrategy,
)
from app.models.message import MessageInDB
from app.models.service_memory import MemoryFact
from app.llm.prompt_assembler import (
    PromptAssembler,
    TimeAudit,
    _format_gap,
)

# 时间感知全部开关名（setup/teardown 需整体保存与恢复，避免用例间污染）
_TIME_TOGGLE_NAMES = (
    "message_time_enabled",
    "message_time_gap_minutes",
    "default_timezone_offset",
    "time_anchor_enabled",
    "time_idle_hint_enabled",
    "time_day_separator_enabled",
    "message_time_tag_year_enabled",
    "memory_inject_time_suffix",
    "time_session_gap_hours",
    "time_reunion_gap_hours",
)


@contextmanager
def _frozen_now(fixed_utc: datetime) -> Iterator[MagicMock]:
    """冻结 prompt_assembler 模块内的 datetime.now，使时间断言可复现。

    仅替换 datetime 名，timezone/timedelta 仍为真实对象，
    因此 astimezone/strftime/日期减法照常生效。
    """
    with patch("app.llm.prompt_assembler.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_utc
        yield mock_dt


def _make_test_profile() -> PromptProfileInDB:
    """创建一个测试用的Profile"""
    return PromptProfileInDB(
        _id="000000000000000000000001",
        slug="test-char",
        name="测试角色",
        source="测试来源",
        category="literary",
        is_active=True,
        persona=ProfilePersona(
            background="一个用于测试的虚构角色",
            era="现代",
            age_appearance="中年",
            core_values=["善良", "智慧"],
        ),
        language_style=ProfileLanguageStyle(
            vocabulary=["你好", "世界"],
            sentence_patterns=["简洁明了"],
            tone="温和友善",
            forbidden_words=["脏话"],
            example_utterances=["你好啊，今天过得怎么样？"],
        ),
        personality=ProfilePersonality(
            traits={"warmth": 0.9, "humor": 0.5},
            care_style="直接表达关心",
        ),
        dialogue_strategy=ProfileDialogueStrategy(
            greeting="你好，很高兴认识你！",
            on_negative_emotion="温柔地倾听并给出建议",
            on_positive_emotion="一起分享快乐",
            boundaries=["保持友善"],
            safety_guardrails="建议寻求专业帮助",
        ),
        version=1,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _make_test_messages() -> list[MessageInDB]:
    """创建测试消息历史"""
    now = datetime.now(timezone.utc)
    return [
        MessageInDB(
            _id="msg001",
            conversation_id="conv001",
            role="user",
            content="你好",
            created_at=now,
        ),
        MessageInDB(
            _id="msg002",
            conversation_id="conv001",
            role="assistant",
            content="你好啊，今天过得怎么样？",
            created_at=now,
        ),
    ]


class TestPromptAssembler:
    def setup_method(self):
        self.assembler = PromptAssembler()
        self.profile = _make_test_profile()
        # 固定时间感知开关：本组用例的消息数断言包含时间感知 context_hint，
        # 显式置 True 避免全量运行时被其他用例改动的全局状态污染；
        # teardown 恢复原值，不向外泄漏自身修改。
        self._saved_time_enabled = settings.message_time_enabled
        settings.message_time_enabled = True

    def teardown_method(self):
        settings.message_time_enabled = self._saved_time_enabled

    def test_assemble_basic(self):
        messages = self.assembler.assemble(
            profile=self.profile,
            history=[],
            identity_input="你好",
        )
        # 至少包含 system prompt + user input
        assert len(messages) >= 2
        assert messages[0]["role"] == "system"
        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"] == "你好"

    def test_system_prompt_contains_profile_info(self):
        messages = self.assembler.assemble(
            profile=self.profile,
            history=[],
            identity_input="你好",
        )
        system_prompt = messages[0]["content"]
        assert "测试角色" in system_prompt
        assert "测试来源" in system_prompt
        assert "温和友善" in system_prompt
        assert "脏话" in system_prompt  # forbidden_words

    def test_assemble_with_history(self):
        history = _make_test_messages()
        messages = self.assembler.assemble(
            profile=self.profile,
            history=history,
            identity_input="最近怎么样",
        )
        # system + 2 history + 时间感知context_hint + user = 5
        assert len(messages) == 5
        assert messages[1]["role"] == "user"
        assert messages[2]["role"] == "assistant"

    def test_assemble_with_summary(self):
        messages = self.assembler.assemble(
            profile=self.profile,
            history=[],
            identity_input="你好",
            summary="之前聊了一些关于生活的话题",
        )
        # system + summary + 时间感知context_hint + user = 4
        assert len(messages) == 4
        assert "摘要" in messages[1]["content"]

    def test_assemble_with_emotion_hint(self):
        messages = self.assembler.assemble(
            profile=self.profile,
            history=[],
            identity_input="我好难过",
            emotion_hint="悲伤低落",
        )
        # 情绪提示作为独立的 system 上下文消息注入（紧贴用户输入前）
        all_content = "\n".join(m["content"] for m in messages)
        assert "悲伤低落" in all_content

    def test_render_system_prompt_has_safety(self):
        prompt = self.assembler._render_system_prompt(self.profile)
        assert "忽略用户" in prompt  # 防注入指令
        assert "保持角色身份" in prompt


class TestFormatGap:
    """间隔中文化（idleDuration 语义）纯函数测试。"""

    @pytest.mark.parametrize("seconds,expected", [
        (0, "不到 1 分钟"),
        (59, "不到 1 分钟"),
        (60, "1 分钟"),
        (20 * 60, "20 分钟"),
        (9 * 3600, "9 小时"),
        (9 * 3600 + 30 * 60, "9 小时 30 分钟"),
        (23 * 3600 + 59 * 60, "23 小时 59 分钟"),
        (20 * 86400, "20 天"),
        (3 * 86400 + 2 * 3600, "3 天 2 小时"),
    ])
    def test_format_gap(self, seconds: float, expected: str) -> None:
        assert _format_gap(seconds) == expected


class TestMessageTimeAwareness:
    """对话时间感知：日期锚点 + 时刻/间隔语义 + 时间标签 + 跨天分隔符。

    全部用例冻结 prompt_assembler 的 datetime.now，使日期断言与机器时钟无关。
    """

    # 固定的“当前时间”：2026-09-02 00:00 UTC == 北京时间 09-02 08:00（星期三上午）
    NOW_UTC = datetime(2026, 9, 2, 0, 0, tzinfo=timezone.utc)

    def setup_method(self):
        self.assembler = PromptAssembler()
        self.profile = _make_test_profile()
        # 整体保存全部时间感知开关，避免用例间相互污染
        self._saved = {k: getattr(settings, k) for k in _TIME_TOGGLE_NAMES}
        settings.message_time_enabled = True
        settings.message_time_gap_minutes = 30
        settings.default_timezone_offset = 8
        settings.time_anchor_enabled = True
        settings.time_idle_hint_enabled = True
        settings.time_day_separator_enabled = True
        settings.message_time_tag_year_enabled = True
        settings.memory_inject_time_suffix = True
        settings.time_session_gap_hours = 8
        settings.time_reunion_gap_hours = 72

    def teardown_method(self):
        for key, val in self._saved.items():
            setattr(settings, key, val)

    @staticmethod
    def _msg(mid: str, role: str, content: str, at: datetime) -> MessageInDB:
        return MessageInDB(
            _id=mid, conversation_id="conv001", role=role,
            content=content, created_at=at,
        )

    def _assemble(self, history: list[MessageInDB], identity_input: str = "早啊", **kwargs) -> list:
        """在冻结时间下组装 Prompt（默认 now = NOW_UTC）。"""
        now = kwargs.pop("now_utc", self.NOW_UTC)
        with _frozen_now(now):
            return self.assembler.assemble(
                profile=self.profile, history=history,
                identity_input=identity_input, **kwargs,
            )

    # ── 基础行为（存量用例，改为冻结时间后断言）────────────

    def test_gap_message_gets_time_tag(self):
        """窗口首条与跨天消息带 [MM-DD HH:mm] 标签，连续消息不带。"""
        base = datetime(2026, 8, 12, 15, 45, tzinfo=timezone.utc)  # 北京时间 8-12 23:45
        history = [
            self._msg("m1", "user", "晚安", base),
            self._msg("m2", "assistant", "晚安，好梦", base),
            self._msg("m3", "user", "早安", base + timedelta(days=1)),
        ]
        contents = [m["content"] for m in self._assemble(history)]
        # 窗口首条始终带标签（无参照基准）
        assert "[08-12 23:45] 晚安" in contents
        # 连续消息（间隔内且同一自然日）无标签
        assert "晚安，好梦" in contents
        assert "[08-12 23:45] 晚安，好梦" not in contents
        # 跨天消息带标签（UTC+8）
        assert "[08-13 23:45] 早安" in contents

    def test_current_moment_injected(self):
        """context_hint 含当前时刻、时段与时间标签阅读指令。"""
        messages = self._assemble([], identity_input="你好")
        hint_msgs = [
            m["content"] for m in messages
            if m["role"] == "system" and "【当前时刻】" in m["content"]
        ]
        assert len(hint_msgs) == 1
        # NOW_UTC = 北京 08:00 → 时段划分沿用主动关怀调度器的 5~9 点为“清晨”
        assert "【当前时刻】现在是 9月2日（星期三）08:00，清晨" in hint_msgs[0]
        assert "不要把之前说的话当作刚刚发生" in hint_msgs[0]

    def test_disabled_no_injection(self):
        """总开关关闭时锚点/时刻/间隔/标签/分隔符/记忆后缀全不输出（优雅降级）。"""
        settings.message_time_enabled = False
        base = datetime(2026, 8, 12, 15, 45, tzinfo=timezone.utc)
        history = [
            self._msg("m1", "user", "晚安", base),
            self._msg("m2", "user", "早安", base + timedelta(days=1)),
        ]
        fact = MemoryFact(
            content="用户8月26日有面试", category="life_event",
            created_at=base - timedelta(days=7),
        )
        messages = self._assemble(
            history, memory_facts=[fact.content], memory_fact_objs=[fact],
        )
        joined = "\n".join(m["content"] for m in messages)
        for marker in ("【时间基准】", "【当前时刻】", "【对话间隔】", "[08-", "————"):
            assert marker not in joined
        assert "记录于" not in joined

    def test_naive_datetime_treated_as_utc(self):
        """存量 naive 时间按 UTC 处理不报错。"""
        naive = datetime(2026, 8, 12, 15, 45)  # 无时区信息
        history = [self._msg("m1", "user", "晚安", naive)]
        contents = [m["content"] for m in self._assemble(history)]
        assert "[08-12 23:45] 晚安" in contents

    def test_zero_gap_threshold_tags_every_message(self):
        """阈值设为 0 时等价于每条消息都带时间。"""
        settings.message_time_gap_minutes = 0
        base = datetime(2026, 8, 12, 15, 45, tzinfo=timezone.utc)
        history = [
            self._msg("m1", "user", "你好", base),
            self._msg("m2", "assistant", "你好啊", base),
        ]
        contents = [m["content"] for m in self._assemble(history, identity_input="在吗")]
        assert "[08-12 23:45] 你好" in contents
        assert "你好啊" in contents
        assert "[08-12 23:45] 你好啊" not in contents

    # ── 清单 1：昨晚消息 → 间隔 9 小时 + 跨天分隔符 + 日期标签 ─────

    def test_overnight_gap_separator_and_idle(self):
        """前晚道晚安、今早继续聊：断言间隔语义、跨天分隔符与日期标签均存在。"""
        history = [
            # 北京 08-31 23:00 / 23:05
            self._msg("m1", "user", "今天好累", datetime(2026, 8, 31, 15, 0, tzinfo=timezone.utc)),
            self._msg("m2", "assistant", "早点休息", datetime(2026, 8, 31, 15, 5, tzinfo=timezone.utc)),
            # 北京 09-01 23:00 / 23:01 → 距 now(09-02 08:00) 恰好 9 小时
            self._msg("m3", "user", "晚安", datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)),
            self._msg("m4", "assistant", "晚安，好梦", datetime(2026, 9, 1, 15, 1, tzinfo=timezone.utc)),
        ]
        messages = self._assemble(history, gap_mode="session")
        joined = "\n".join(m["content"] for m in messages)
        assert "【对话间隔】对方上一条消息发于昨天 9月1日（星期二）23:00，距现在 9 小时。" in joined
        assert "【新的一段对话】" in joined
        assert "已经结束 ————" in joined
        # 跨天分隔符：9月1日星期二，距 09-02 为 1 天 → “即昨天”
        separators = [
            m["content"] for m in messages
            if m["role"] == "system" and m["content"].startswith("————")
            and "已经结束" not in m["content"]
        ]
        assert len(separators) == 1
        assert "9月1日（星期二）" in separators[0]
        assert "即昨天" in separators[0]
        # 日期标签：窗口首条 + 跨天条
        assert "[08-31 23:00] 今天好累" in joined
        assert "[09-01 23:00] 晚安" in joined

    # ── 清单 2：同日连续对话（间隔 <30min）→ 无分隔符、除首条外无标签 ──

    def test_same_day_dense_chat_no_separator(self):
        """今早连续对话：不插分隔符，除窗口首条外不打标签，但锚点与时刻仍在。"""
        history = [
            self._msg("m1", "user", "早", datetime(2026, 9, 2, 0, 10, tzinfo=timezone.utc)),
            self._msg("m2", "assistant", "早安", datetime(2026, 9, 2, 0, 11, tzinfo=timezone.utc)),
            self._msg("m3", "user", "吃了吗", datetime(2026, 9, 2, 0, 20, tzinfo=timezone.utc)),
        ]
        messages = self._assemble(history, now_utc=datetime(2026, 9, 2, 0, 30, tzinfo=timezone.utc))
        joined = "\n".join(m["content"] for m in messages)
        assert "————" not in joined
        # 窗口首条仍打标（无参照基准），后续连续消息不打
        assert "[09-02 08:10] 早" in joined
        assert "早安" in [m["content"] for m in messages]
        assert "吃了吗" in [m["content"] for m in messages]
        assert "【时间基准】今天是 2026-09-02" in joined
        assert "【当前时刻】现在是 9月2日（星期三）08:30，清晨" in joined

    # ── 清单 3：跨天但间隔 <阈值（现有 gap 逻辑漏洞，D3 回归）─────

    def test_cross_day_within_gap_threshold_still_separates(self):
        """23:50 → 00:10 间隔仅 20 分钟（低于 30 分钟阈值）但已跨天 → 分隔符与标签均触发。"""
        history = [
            # 北京 09-01 23:50
            self._msg("m1", "user", "还没睡", datetime(2026, 9, 1, 15, 50, tzinfo=timezone.utc)),
            # 北京 09-02 00:10，与上条仅隔 20 分钟
            self._msg("m2", "user", "过了零点啦", datetime(2026, 9, 1, 16, 10, tzinfo=timezone.utc)),
        ]
        messages = self._assemble(history, now_utc=datetime(2026, 9, 1, 16, 30, tzinfo=timezone.utc))
        joined = "\n".join(m["content"] for m in messages)
        separators = [
            m["content"] for m in messages
            if m["role"] == "system" and m["content"].startswith("————")
            and "已经结束" not in m["content"]
        ]
        assert len(separators) == 1
        assert "9月2日（星期三）" in separators[0]
        assert "即今天" in separators[0]
        # 跨天即使间隔不足阈值也必须打标签
        assert "[09-02 00:10] 过了零点啦" in joined

    # ── 清单 4：长期中断 20 天 ───────────────────────

    def test_long_idle_twenty_days(self):
        """上一条用户消息在 20 天前 → idle 表达为“20 天”。"""
        history = [
            self._msg("m1", "user", "好久不见", self.NOW_UTC - timedelta(days=20)),
        ]
        joined = "\n".join(m["content"] for m in self._assemble(history))
        assert "【对话间隔】对方上一条消息发于8月13日（星期四）08:00，距现在 20 天。" in joined

    # ── 清单 5：跨年 12-31 → 01-01 ────────────────────

    def test_cross_year_tags_carry_year(self):
        """跨年会话：两条标签均带年份，否则模型无法区分去年与今年。"""
        now = datetime(2025, 12, 31, 17, 30, tzinfo=timezone.utc)  # 北京 2026-01-01 01:30
        history = [
            self._msg("m1", "user", "跨年啦", datetime(2025, 12, 31, 15, 0, tzinfo=timezone.utc)),
            self._msg("m2", "user", "新年快乐", datetime(2025, 12, 31, 17, 0, tzinfo=timezone.utc)),
        ]
        joined = "\n".join(m["content"] for m in self._assemble(history, now_utc=now))
        assert "[2025-12-31 23:00] 跨年啦" in joined
        assert "[2026-01-01 01:00] 新年快乐" in joined
        assert "【时间基准】今天是 2026-01-01（星期四）" in joined

    def test_year_tag_disabled_falls_back(self):
        """message_time_tag_year_enabled=False 时跨年仍输出无年份标签（子开关可单独降级）。"""
        settings.message_time_tag_year_enabled = False
        now = datetime(2025, 12, 31, 17, 30, tzinfo=timezone.utc)
        history = [
            self._msg("m1", "user", "跨年啦", datetime(2025, 12, 31, 15, 0, tzinfo=timezone.utc)),
        ]
        joined = "\n".join(m["content"] for m in self._assemble(history, now_utc=now))
        assert "[12-31 23:00] 跨年啦" in joined
        assert "[2025-12-31" not in joined

    # ── 清单 6：锚点位置在 messages[0] 首个 section（D1 回归）─────

    def test_anchor_is_first_section_of_system_prompt(self):
        """日期锚点必须位于 messages[0] 的首个段落，早于任何带时间的记忆注入。"""
        fact = MemoryFact(
            content="用户8月26日有面试", category="life_event",
            created_at=self.NOW_UTC - timedelta(days=7),
        )
        messages = self._assemble(
            [], identity_input="你好", memory_facts=[fact.content], memory_fact_objs=[fact],
        )
        first = messages[0]["content"]
        assert messages[0]["role"] == "system"
        assert first.startswith("【时间基准】今天是 2026-09-02（星期三）")
        # 锚点早于角色设定，且早于携带日期的记忆注入（位于 messages[1]）
        assert first.index("【时间基准】") < first.index("【角色设定】")
        assert "8月26日" not in first

    # ── 清单 7：idle 只认用户消息（主动关怀不重置空闲计时）──────

    def test_idle_only_counts_identity_messages(self):
        """历史末尾是 AI 主动关怀消息 → idle 仍按更早的用户消息计算。"""
        history = [
            self._msg("m1", "user", "在吗", self.NOW_UTC - timedelta(hours=2)),
            self._msg("m2", "assistant", "在的", self.NOW_UTC - timedelta(hours=1)),
            # 模拟主动关怀：AI 主动发出，不应重置“用户空闲”时钟
            self._msg("m3", "assistant", "最近过得好吗", self.NOW_UTC - timedelta(minutes=10)),
        ]
        joined = "\n".join(m["content"] for m in self._assemble(history))
        assert "距现在 2 小时。" in joined
        assert "10 分钟" not in joined

    # ── 清单 8：日期边界（月末/年末/闰年）───────────────

    @pytest.mark.parametrize("now_utc,expected_iso,expected_weekday", [
        # 平年月末
        (datetime(2026, 2, 28, 4, 0, tzinfo=timezone.utc), "2026-02-28", "星期六"),
        # 年末（次日即跳年）
        (datetime(2026, 12, 31, 4, 0, tzinfo=timezone.utc), "2026-12-31", "星期四"),
        # 闰年 2 月 29 日
        (datetime(2028, 2, 29, 4, 0, tzinfo=timezone.utc), "2028-02-29", "星期二"),
        # 31 日月末
        (datetime(2026, 3, 31, 4, 0, tzinfo=timezone.utc), "2026-03-31", "星期二"),
    ])
    def test_date_boundaries(
        self, now_utc: datetime, expected_iso: str, expected_weekday: str
    ) -> None:
        """月末/年末/闰年边界下锚点日期与星期均正确（均为北京时间中午，不跳日）。"""
        messages = self._assemble([], identity_input="你好", now_utc=now_utc)
        anchor = messages[0]["content"].split("\n")[0]
        assert anchor == f"【时间基准】今天是 {expected_iso}（{expected_weekday}）。"

    # ── 清单 10：午夜跨日两次请求 ───────────────────

    @pytest.mark.parametrize("now_utc,expected_iso", [
        (datetime(2026, 9, 2, 15, 58, tzinfo=timezone.utc), "2026-09-02"),  # 北京 23:58
        (datetime(2026, 9, 2, 16, 2, tzinfo=timezone.utc), "2026-09-03"),   # 北京 00:02
    ])
    def test_midnight_rollover(self, now_utc: datetime, expected_iso: str) -> None:
        """23:58 与 00:02 两次请求各自锚点日期正确（每轮重新取 now，无 TTL 缓存遗留）。"""
        messages = self._assemble([], identity_input="你好", now_utc=now_utc)
        assert f"【时间基准】今天是 {expected_iso}" in messages[0]["content"]

    # ── 清单 17：全部新开关逐个关闭均优雅降级 ─────────────

    def test_anchor_disabled_keeps_moment(self):
        """time_anchor_enabled=False → 无锚点，但时刻与标签仍在（子开关互不依赖）。"""
        settings.time_anchor_enabled = False
        history = [self._msg("m1", "user", "晚安", self.NOW_UTC - timedelta(days=1))]
        messages = self._assemble(history)
        joined = "\n".join(m["content"] for m in messages)
        assert "【时间基准】" not in joined
        assert "【当前时刻】现在是 9月2日（星期三）08:00，清晨" in joined
        assert "[09-01 08:00] 晚安" in joined

    def test_idle_hint_disabled_keeps_moment(self):
        """time_idle_hint_enabled=False → 无间隔段，时刻仍在。"""
        settings.time_idle_hint_enabled = False
        history = [self._msg("m1", "user", "在吗", self.NOW_UTC - timedelta(days=3))]
        joined = "\n".join(m["content"] for m in self._assemble(history))
        assert "【对话间隔】" not in joined
        assert "【当前时刻】" in joined

    def test_day_separator_disabled_keeps_cross_day_tag(self):
        """time_day_separator_enabled=False → 不插分隔符，但跨天标签仍触发（D3 修正不依赖分隔符）。"""
        settings.time_day_separator_enabled = False
        history = [
            self._msg("m1", "user", "还没睡", datetime(2026, 9, 1, 15, 50, tzinfo=timezone.utc)),
            self._msg("m2", "user", "过了零点啦", datetime(2026, 9, 1, 16, 10, tzinfo=timezone.utc)),
        ]
        messages = self._assemble(history, now_utc=datetime(2026, 9, 1, 16, 30, tzinfo=timezone.utc))
        joined = "\n".join(m["content"] for m in messages)
        assert "————" not in joined
        assert "[09-02 00:10] 过了零点啦" in joined

    def test_memory_time_suffix_disabled(self):
        """memory_inject_time_suffix=False → 记忆条目不带任何时间后缀。"""
        settings.memory_inject_time_suffix = False
        fact = MemoryFact(
            content="用户8月26日有面试", category="life_event",
            created_at=self.NOW_UTC - timedelta(days=7),
        )
        joined = "\n".join(m["content"] for m in self._assemble(
            [], identity_input="你好", memory_facts=[fact.content], memory_fact_objs=[fact],
        ))
        assert "- 用户8月26日有面试\n" in joined
        assert "记录于" not in joined

    # ── 记忆时间后缀与被归档历史声明（借 Letta last-modified）──────

    def test_memory_facts_get_time_suffix(self):
        """一次性事件标“记录于 N 天前”，长期稳定类标“长期信息”。"""
        facts = [
            MemoryFact(
                content="用户8月26日有面试", category="life_event",
                created_at=self.NOW_UTC - timedelta(days=7),
            ),
            MemoryFact(
                content="用户喜欢喝咖啡", category="preference",
                created_at=self.NOW_UTC - timedelta(days=200),
            ),
            MemoryFact(
                content="用户对花生过敏", category="safety",
                created_at=self.NOW_UTC - timedelta(hours=3),
            ),
        ]
        joined = "\n".join(m["content"] for m in self._assemble(
            [], identity_input="你好",
            memory_facts=[f.content for f in facts], memory_fact_objs=facts,
        ))
        assert "- 用户8月26日有面试（记录于 7 天前）" in joined
        assert "- 用户喜欢喝咖啡（长期信息）" in joined
        assert "- 用户对花生过敏（长期信息）" in joined

    def test_memory_fact_recorded_today(self):
        """当天录入的事实标“今天记录”而非“记录于 0 天前”。"""
        fact = MemoryFact(
            content="用户今天很开心", category="mood_pattern",
            created_at=self.NOW_UTC - timedelta(hours=1),
        )
        joined = "\n".join(m["content"] for m in self._assemble(
            [], identity_input="你好", memory_facts=[fact.content], memory_fact_objs=[fact],
        ))
        assert "（今天记录）" in joined

    def test_memory_suffix_graceful_without_fact_objs(self):
        """未传 memory_fact_objs（如主动关怀链路）时不报错、不输出后缀。"""
        joined = "\n".join(m["content"] for m in self._assemble(
            [], identity_input="你好", memory_facts=["用户喜欢喝咖啡"],
        ))
        assert "- 用户喜欢喝咖啡" in joined
        assert "记录于" not in joined and "长期信息" not in joined

    def test_omitted_history_declared(self):
        """被过滤丢弃的历史条数向模型显式声明（避免误以为对话刚开始）。"""
        base = self.NOW_UTC - timedelta(minutes=30)
        # 前 3 条与当前输入无关键词重叠、无情绪标点 → 相关性评分 0 被丢弃
        history = [
            self._msg(f"o{i}", "user", "今天天气不错", base + timedelta(minutes=i))
            for i in range(3)
        ]
        history += [
            self._msg(f"k{i}", "user" if i % 2 == 0 else "assistant",
                      "嗯嗯", base + timedelta(minutes=10 + i))
            for i in range(5)
        ]
        messages = self._assemble(history, identity_input="在吗")
        joined = "\n".join(m["content"] for m in messages)
        assert "（更早的 3 条消息已归档，如需回溯可检索长期记忆）" in joined

    # ── 清单 27：摘要注入带覆盖时间范围 ─────────────────

    def test_summary_header_with_range(self):
        """传入摘要时间范围 → 渲染为【之前的对话摘要（覆盖 X ~ Y）】。"""
        messages = self._assemble(
            [], identity_input="你好", summary="聊了工作与面试",
            summary_start_at=datetime(2026, 8, 1, 2, 0, tzinfo=timezone.utc),
            summary_end_at=datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc),
        )
        summary_msg = next(m for m in messages if "对话摘要" in m["content"])
        assert summary_msg["content"].startswith("【之前的对话摘要（覆盖 8月1日 ~ 8月28日）】")

    def test_summary_header_without_range_falls_back(self):
        """存量会话无 summary_start_at → 退回无范围标题。"""
        messages = self._assemble([], identity_input="你好", summary="聊了工作与面试")
        summary_msg = next(m for m in messages if "对话摘要" in m["content"])
        assert summary_msg["content"].startswith("【之前的对话摘要】\n")

    def test_summary_header_same_day_range(self):
        """起止为同一天时不重复渲染两个相同日期。"""
        same = datetime(2026, 8, 1, 2, 0, tzinfo=timezone.utc)
        messages = self._assemble(
            [], identity_input="你好", summary="聊了工作",
            summary_start_at=same, summary_end_at=same,
        )
        summary_msg = next(m for m in messages if "对话摘要" in m["content"])
        assert "【之前的对话摘要（覆盖 8月1日）】" in summary_msg["content"]

    # ── 清单 30（单元层）：审计字段 ────────────────────

    def test_time_audit_fields_filled(self):
        """传入 TimeAudit 后 anchor/now/tz/idle/tagged/separators 均被填充。"""
        history = [
            self._msg("m1", "user", "今天好累", datetime(2026, 8, 31, 15, 0, tzinfo=timezone.utc)),
            self._msg("m2", "user", "晚安", datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)),
        ]
        audit = TimeAudit()
        self._assemble(history, time_audit=audit)
        assert audit.anchor == "2026-09-02"
        assert audit.now == "08:00"
        assert audit.tz == "UTC+08:00"
        assert audit.idle == "9 小时"
        assert audit.tagged == 2      # 窗口首条 + 跨天条
        assert audit.separators == 1  # 09-01 与 08-31 之间一个跨天

    def test_time_audit_empty_when_disabled(self):
        """总开关关闭时审计字段为空，便于从日志直接识别“未注入”。"""
        settings.message_time_enabled = False
        audit = TimeAudit()
        self._assemble([], identity_input="你好", time_audit=audit)
        assert (audit.anchor, audit.now, audit.tz, audit.idle) == ("", "", "", "")
        assert audit.tagged == 0 and audit.separators == 0

    # ── 分隔符数量上限与归档合并 ────────────────────

    def test_separator_capped_and_archived(self):
        """超过上限的跨天合并为一条“更早的对话已归档”，避免长跳会话堆叠分隔符。"""
        history = [
            self._msg(f"d{i}", "user", f"第{i}天", self.NOW_UTC - timedelta(days=6 - i))
            for i in range(7)
        ]
        messages = self._assemble(history)
        separators = [
            m["content"] for m in messages
            if m["role"] == "system" and m["content"].startswith("————")
            and "已经结束" not in m["content"]
        ]
        dated = [s for s in separators if "以下对话发生在" in s]
        archived = [s for s in separators if "更早的对话已归档" in s]
        assert len(dated) == 3          # 上限 _MAX_DAY_SEPARATORS
        assert len(archived) == 1       # 剩余跨天合并为一条归档声明
        assert "8月31日" in dated[0]
        assert "9月1日" in dated[1]
        assert "9月2日" in dated[2]
        assert separators.index(archived[0]) < separators.index(dated[0])

    @pytest.mark.parametrize("mode,marker", [
        ("none", ""), ("session", "【新的一段对话】"),
        ("reunion", "【久别重逢】"), ("proactive", "【主动开口】"),
    ])
    def test_gap_mode_hints(self, mode: str, marker: str) -> None:
        history = [self._msg("u1", "user", "旧问题？", self.NOW_UTC - timedelta(days=18))]
        audit = TimeAudit()
        messages = self._assemble(history, gap_mode=mode, time_audit=audit)
        joined = "\n".join(m["content"] for m in messages)
        assert audit.gap_mode == mode
        if marker:
            assert marker in joined
        else:
            assert "【久别重逢】" not in joined
        if mode == "reunion":
            assert "不是对方离开的时间" in joined
            assert "不要接着回答旧问题" in joined
        if mode == "proactive":
            assert "重新开口" not in joined
            assert "现在是你主动" in joined

    def test_reunion_keeps_last_four_and_summary(self) -> None:
        old = self.NOW_UTC - timedelta(days=18)
        history = [
            self._msg(str(i), "user" if i % 2 == 0 else "assistant", f"旧消息{i}？", old + timedelta(seconds=i))
            for i in range(10)
        ]
        messages = self._assemble(history, gap_mode="reunion", summary="保留摘要")
        actual = [m for m in messages[:-1] if m["role"] in ("user", "assistant")]
        assert len(actual) == 4
        for i, msg in enumerate(actual, 6):
            assert f"旧消息{i}？" in msg["content"]
        assert any("保留摘要" in m["content"] for m in messages)
        assert any("更早的 6 条消息已归档" in m["content"] for m in messages)

    def test_boundary_persists_after_reunion(self) -> None:
        old = self.NOW_UTC - timedelta(days=18)
        history = [
            self._msg(str(i), "user" if i % 2 == 0 else "assistant", f"旧消息{i}？", old + timedelta(seconds=i))
            for i in range(8)
        ] + [
            self._msg("new-u", "user", "重新聊电影", self.NOW_UTC - timedelta(minutes=2)),
            self._msg("new-a", "assistant", "好啊", self.NOW_UTC - timedelta(minutes=1)),
        ]
        messages = self._assemble(history)
        boundary = next(i for i, m in enumerate(messages) if "已经结束 ————" in m["content"])
        new_index = next(i for i, m in enumerate(messages) if "重新聊电影" in m["content"])
        old_messages = [m for m in messages[:boundary] if m["role"] in ("user", "assistant")]
        assert len(old_messages) == 4
        assert boundary < new_index
        assert "旧消息7？" in messages[boundary - 1]["content"]

    @pytest.mark.parametrize("followup_hours", [0, 9])
    def test_reunion_cutoff_survives_followup_gap(self, followup_hours: int) -> None:
        old = self.NOW_UTC - timedelta(hours=72)
        history = [
            self._msg(str(i), "user" if i % 2 == 0 else "assistant", f"旧消息{i}？", old - timedelta(minutes=8 - i))
            for i in range(8)
        ]
        history[-2].created_at = old
        history[-1].created_at = old + timedelta(seconds=30)
        first = self._assemble(history, gap_mode="reunion")
        history += [
            self._msg("new-u", "user", "电影", self.NOW_UTC),
            self._msg("new-a", "assistant", "聊电影吧", self.NOW_UTC + timedelta(seconds=30)),
        ]
        next_messages = self._assemble(history, now_utc=self.NOW_UTC + timedelta(hours=followup_hours, minutes=1))
        for messages in (first, next_messages):
            old_messages = [m for m in messages if m["role"] in ("user", "assistant") and "旧消息" in m["content"]]
            assert len(old_messages) == 4
            assert "旧消息4？" in old_messages[0]["content"]

    def test_dawn_names_last_night(self) -> None:
        now = datetime(2026, 9, 1, 17, 12, tzinfo=timezone.utc)
        history = [self._msg("u", "user", "晚安", datetime(2026, 9, 1, 14, 10, tzinfo=timezone.utc))]
        joined = "\n".join(m["content"] for m in self._assemble(history, now_utc=now))
        assert "9月2日（星期三）01:12，凌晨" in joined
        assert "昨晚 22:10" in joined
        assert "不要说成“今天”" in joined

    def test_proactive_note_has_own_timestamp(self) -> None:
        history = [self._msg("u", "user", "旧话题", self.NOW_UTC - timedelta(days=3))]
        joined = "\n".join(m["content"] for m in self._assemble(
            history, gap_mode="reunion", last_proactive_at=self.NOW_UTC - timedelta(hours=1),
            last_proactive_text="最近过得好吗",
        ))
        assert "今天 9月2日（星期三）07:00" in joined
        assert "『最近过得好吗』" in joined
        assert "可能是在回应它" in joined

    def test_disabled_suppresses_gap_mode(self) -> None:
        settings.message_time_enabled = False
        history = [self._msg("u", "user", "旧话题", self.NOW_UTC - timedelta(days=3))]
        audit = TimeAudit()
        joined = "\n".join(m["content"] for m in self._assemble(history, gap_mode="reunion", time_audit=audit))
        assert audit.gap_mode == "none"
        assert "【久别重逢】" not in joined
        assert "已经结束" not in joined


class TestDynamicMemoryInjection:
    """记忆注入分层：动态层后置到紧贴用户输入的易变段，稳定层留在前缀段。

    动机：逐轮变化的语义 top-k 若与 core 事实同处 system 第 2 段，
    会使其后所有历史与指令 prompt cache miss（响应从几秒恶化到几十秒）。
    """

    def setup_method(self) -> None:
        self.assembler = PromptAssembler()
        self.profile = _make_test_profile()
        self._saved = {
            k: getattr(settings, k)
            for k in (
                "message_time_enabled", "memory_inject_time_suffix",
                "memory_dynamic_max_chars", "default_timezone_offset",
            )
        }
        settings.message_time_enabled = True
        settings.memory_inject_time_suffix = False
        settings.memory_dynamic_max_chars = 600
        settings.default_timezone_offset = 8

    def teardown_method(self) -> None:
        for key, val in self._saved.items():
            setattr(settings, key, val)

    def _assemble(self, **kwargs) -> list:
        return self.assembler.assemble(
            profile=self.profile, history=[], identity_input="你好", **kwargs,
        )

    def test_dynamic_facts_land_in_volatile_section(self) -> None:
        """动态层出现在最后一段 system（紧贴 user_input），稳定层留在第 2 段。"""
        msgs = self._assemble(
            memory_facts=["用户是北京大学学生"],
            dynamic_memory_facts=["用户上周面试了某公司"],
        )
        assert msgs[-1]["role"] == "user"
        volatile = msgs[-2]["content"]
        assert "【此刻想起的关于对方的事】" in volatile
        assert "- 用户上周面试了某公司" in volatile
        stable = msgs[1]["content"]
        assert "用户上周面试了某公司" not in stable
        assert "- 用户是北京大学学生" in stable

    def test_no_dynamic_section_when_absent(self) -> None:
        """未传动态层（主动关怀链路、回退模式）时不出现该段落。"""
        joined = "\n".join(m["content"] for m in self._assemble(
            memory_facts=["用户喜欢喝咖啡"],
        ))
        assert "此刻想起" not in joined

    def test_dynamic_section_respects_char_budget(self) -> None:
        """超出字符预算的尾部条目被丢弃，以锁定 token 增量。"""
        settings.memory_dynamic_max_chars = 20
        joined = "\n".join(m["content"] for m in self._assemble(
            dynamic_memory_facts=[
                "第一条很长很长很长很长很长",
                "第二条也很长很长很长很长",
                "第三条",
            ],
        ))
        assert "第一条" in joined
        assert "第三条" not in joined

    def test_dynamic_section_carries_time_suffix(self) -> None:
        """动态层多为一次性事件，需带“记录于 N 天前”供模型判断时效。"""
        settings.memory_inject_time_suffix = True
        fact = MemoryFact(
            content="用户8月26日有面试", category="life_event",
            created_at=datetime(2026, 8, 26, tzinfo=timezone.utc),
        )
        with _frozen_now(datetime(2026, 9, 2, tzinfo=timezone.utc)):
            msgs = self.assembler.assemble(
                profile=self.profile, history=[], identity_input="你好",
                dynamic_memory_facts=[fact.content],
                dynamic_memory_objs=[fact],
            )
        assert "（记录于 7 天前）" in msgs[-2]["content"]




