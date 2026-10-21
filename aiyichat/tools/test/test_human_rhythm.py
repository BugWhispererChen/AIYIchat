"""human_rhythm 节奏层单元测试。

核心契约两条，其余都是围绕它们的边界与形状：
1. legacy 档必须与改动前 Bot 侧公式 ``min(len*per_char, max_delay)`` 逐字等价
   ——C1 号称"零行为变更"全靠这条撑着；
2. human 档的形状（下界/上界/单调/昼夜/能量）必须可离线重放，用固定 seed 断言，
   不靠"线上聊几句感觉一下"。
"""

from random import Random

from app.conversation.human_rhythm import (
    RhythmConfig,
    message_gap_ms,
    pacing_delay_seconds,
    thinking_ms,
)

LEGACY = RhythmConfig.legacy(0.05, 2.0)
HUMAN = RhythmConfig(human_mode=True)
DET = HUMAN.without_randomness()
# 放宽上界才能看清系数作用（否则一切都被 clamp 到 2600）
WIDE = RhythmConfig(human_mode=True, gap_max_ms=999_999).without_randomness()


def gap(text: str, cfg: RhythmConfig, **kw: object) -> int:
    return message_gap_ms(text, cfg, rng=Random(0), **kw)  # type: ignore[arg-type]


class TestLegacyEquivalence:
    """legacy 档与改动前公式等价（零行为变更证明）。"""

    def test_matches_old_formula_exactly(self) -> None:
        for n in (0, 1, 5, 10, 20, 39, 40, 41, 80, 200, 5000):
            assert gap("字" * n, LEGACY) == int(min(n * 0.05, 2.0) * 1000), n

    def test_known_sample_values(self) -> None:
        assert [gap("字" * n, LEGACY) for n in (10, 30, 40, 80)] == [
            500, 1500, 2000, 2000,
        ]

    def test_legacy_frames_are_gone_by_design(self) -> None:
        # 逐字浮现被否决：真实 IM 气泡整条出现，逐字长出来反而最像 AI
        assert not hasattr(RhythmConfig(), "chars_per_frame")
        assert not hasattr(RhythmConfig(), "frame_total_min_ms")

    def test_thinking_and_legacy(self) -> None:
        assert thinking_ms(LEGACY, rng=Random(1), input_chars=50) == 0


class TestHumanGapShape:
    """human 档气泡间隔的形状。"""

    def test_absolute_values_locked(self) -> None:
        assert gap("嗯嗯", DET) == 700  # 触下界
        assert gap("字" * 20, DET) == 1721
        assert gap("字" * 30, DET) == 2597
        assert gap("字" * 60, DET) == 2600  # 封顶，长消息不该让人干等

    def test_hard_bounds_across_seeds(self) -> None:
        for n in (0, 5, 30, 120, 1000):
            for seed in range(120):
                g = message_gap_ms("字" * n, HUMAN, rng=Random(seed))
                assert 700 <= g <= 2600 + 9000, (n, seed, g)

    def test_deterministic_within_bounds(self) -> None:
        for n in (0, 5, 30, 120, 1000):
            assert 700 <= gap("字" * n, DET) <= 2600

    def test_same_seed_reproduces(self) -> None:
        assert message_gap_ms("x" * 30, HUMAN, rng=Random(7)) == message_gap_ms(
            "x" * 30, HUMAN, rng=Random(7)
        )

    def test_monotonic_before_cap(self) -> None:
        assert (
            gap("字" * 5, WIDE)
            < gap("字" * 15, WIDE)
            < gap("字" * 35, WIDE)
            < gap("字" * 90, WIDE)
        )

    def test_jitter_actually_dispersion(self) -> None:
        vals = {message_gap_ms("字" * 12, HUMAN, rng=Random(s)) for s in range(50)}
        assert len(vals) > 10

    def test_long_pause_fires_occasionally(self) -> None:
        over = sum(
            1 for s in range(200) if message_gap_ms("字" * 3, HUMAN, rng=Random(s)) > 2600
        )
        assert 20 < over < 120, over  # 约 25% 概率，既不是从不也不是总发生


class TestFactors:
    """昼夜与能量系数。"""

    def test_night_is_1_6x_within_window(self) -> None:
        day = gap("字" * 20, WIDE)
        assert abs(gap("字" * 20, WIDE, hour=3) - day * 1.6) <= 1.0

    def test_window_is_half_open(self) -> None:
        assert gap("字" * 20, WIDE, hour=2) == gap("字" * 20, WIDE, hour=3)
        assert gap("字" * 20, WIDE, hour=7) == gap("字" * 20, WIDE, hour=15)
        assert gap("字" * 20, WIDE, hour=1) == gap("字" * 20, WIDE, hour=15)

    def test_full_energy_is_neutral(self) -> None:
        # 满能量必须恒为 1.0，否则 85ms/字 的校准会被整体抬高
        assert gap("字" * 20, WIDE, energy=1.0) == gap("字" * 20, WIDE)

    def test_zero_energy_slows_by_20_percent(self) -> None:
        assert abs(gap("字" * 20, WIDE, energy=0.0) - gap("字" * 20, WIDE) * 0.8) <= 1.0

    def test_energy_clamped(self) -> None:
        assert gap("字" * 20, WIDE, energy=5.0) == gap("字" * 20, WIDE)
        assert gap("字" * 20, WIDE, energy=-3.0) == gap("字" * 20, WIDE, energy=0.0)


class TestThinking:
    """首条气泡前的思考延迟。"""

    def test_grows_with_input_length(self) -> None:
        assert thinking_ms(DET, rng=Random(0), input_chars=200) > thinking_ms(
            DET, rng=Random(0), input_chars=5
        )

    def test_capped(self) -> None:
        cap = int((900 + 2500) * (1 + HUMAN.jitter_ratio))
        assert max(
            thinking_ms(HUMAN, rng=Random(s), input_chars=99999) for s in range(200)
        ) <= cap

    def test_empty_input_is_zero_in_deterministic_mode(self) -> None:
        assert thinking_ms(DET, rng=Random(0), input_chars=0) == 0

    def test_night_slows_thinking(self) -> None:
        cfg = RhythmConfig(human_mode=True, jitter_ratio=0.0, long_pause_prob=0.0)
        day = thinking_ms(cfg, rng=Random(5), input_chars=40, hour=15)
        night = thinking_ms(cfg, rng=Random(5), input_chars=40, hour=3)
        assert abs(night - day * 1.6) <= 1.0


class TestFromSettings:
    """配置构造：缺键回落、开启取新键、共用入口。"""

    class _Base:
        multi_message_delay_per_char = 0.05
        multi_message_max_delay = 2.0

    class _Enabled(_Base):
        human_rhythm_enabled = True
        human_typing_ms_per_char = 90
        human_gap_min_ms = 800
        human_gap_max_ms = 3000
        human_startup_min_ms = 300
        human_startup_max_ms = 800
        human_jitter_ratio = 0.1
        human_night_factor = 1.5
        human_long_pause_prob = 0.3

    def test_missing_switch_falls_back_to_legacy(self) -> None:
        cfg = RhythmConfig.from_settings(self._Base())
        assert cfg.human_mode is False
        assert message_gap_ms("字" * 30, cfg, rng=Random(0)) == 1500

    def test_enabled_reads_new_keys(self) -> None:
        cfg = RhythmConfig.from_settings(self._Enabled())
        assert cfg.human_mode is True
        assert (cfg.ms_per_char, cfg.gap_min_ms, cfg.gap_max_ms) == (90.0, 800, 3000)

    def test_pacing_entrypoint_matches_legacy(self) -> None:
        # 适配器共用入口：默认档下逐条间隔应与旧的秒级公式一致
        assert pacing_delay_seconds("字" * 30, self._Base()) == 1.5
        assert pacing_delay_seconds("字" * 80, self._Base()) == 2.0
