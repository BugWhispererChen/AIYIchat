"""拟人节奏层：把"思考延迟、气泡间隔、逐字吐字时长"收敛为一组可注入随机源的纯函数。

设计动机：原先 Bot 侧（QQ/微信适配器）与网页侧各算一套打字延迟，网页流式链路甚至完全
没有节奏（气泡出现速度等于 token 生成速度）。本模块只暴露纯函数，随机源与时刻都由调用方
注入，因此可离线重放验证（固定 seed 断言曲线形状），不依赖任何全局状态与模型调用。

两档语义要分清，混用会把节奏拖慢一倍：
- ``message_gap_ms``：**气泡之间**的发送停顿。真人 IM 里整条气泡是突然出现、下一条隔一会儿
  才来，所以这是"发完一条歇多久"，取 700~2600ms 一档。
- ``typing_frames``：**气泡内部**逐字长出来的时长。真实 IM 不做逐字浮现，逐字效果是我们的
  网页端可选呈现方式，默认不启用；只有开启时才按每字时长计算。

参数取值为业界开源配方（均为 MIT 许可，只取参数与算法，不抄代码）：
- ai-companion-kit/humanize.js：msPerChar=85、gap[700,2600]、jitter0.2、burstMax=4、
  25% 概率长停顿 +3~9s、深夜 2–7 点 ×1.6
- state-agent-human-bot/persona.default.json：startup 250~1100ms、长句额外耗时曲线、
  能量系数、昼夜系数

默认由 ``RhythmConfig.legacy`` 构造，公式在 legacy 档与改动前的
``min(len(text) * per_char, max_delay)`` 逐字等价，因此上线本身不改变既有行为。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from random import Random
from typing import Any

# 进程级共享随机源：节奏抖动只需要"不规律"，不需要可复现；
# 单测一律显式注入自己的 Random(seed)，不依赖它。
_shared_rng = Random()


@dataclass(frozen=True)
class RhythmConfig:
    """节奏参数集合。

    ``human_mode=False`` 时只有 ``ms_per_char`` 与 ``gap_max_ms`` 参与计算（等价旧行为），
    其余扩展项一律被忽略或置零；``human_mode=True`` 才启用启动延迟、长句曲线、
    能量/昼夜系数、抖动与偶发长停顿。
    """

    human_mode: bool = False
    ms_per_char: float = 85.0  # 每字时长（毫秒），仅 human 逐字与 legacy 间隔使用
    gap_min_ms: int = 700  # 气泡间隔下界
    gap_max_ms: int = 2600  # 气泡间隔上界（legacy 档即 multi_message_max_delay）
    startup_min_ms: int = 250  # 启动延迟下界（真人不会瞬间敲出下一条）
    startup_max_ms: int = 900  # 启动延迟上界
    curve_cap: float = 0.3  # 长句额外耗时上限比例，0 表示关闭
    curve_div: int = 400  # 长句曲线分母（每 curve_div 字触发 curve_gain 比例加成）
    curve_gain: float = 0.25  # 长句曲线增益比例
    jitter_ratio: float = 0.2  # ±抖动比例，0 表示确定性输出
    night_factor: float = 1.6  # 深夜时段时长倍率（深夜更慢）
    night_start_hour: int = 2
    night_end_hour: int = 7  # 半开区间 [night_start_hour, night_end_hour)
    energy_slowdown: float = 0.2  # 零能量时的最大减速比例，满能量恒为 1.0（不污染每字时长校准）
    long_pause_prob: float = 0.25  # 偶发长停顿概率（真人会中途去干别的）
    long_pause_min_ms: int = 3000
    long_pause_max_ms: int = 9000
    think_per_input_char_ms: int = 8  # 按对方消息长度追加的思考时长
    think_cap_ms: int = 2500  # 思考时长上限
    chars_per_frame: int = 6  # 网页逐帧吐字的每帧字符数
    frame_total_min_ms: int = 300  # 逐帧整条总时长下界（防止短消息一闪而过）
    frame_total_max_ms: int = 8000  # 逐帧整条总时长上界（防止长消息拖太久）

    @classmethod
    def legacy(
        cls, per_char_seconds: float, max_delay_seconds: float
    ) -> RhythmConfig:
        """由既有 ``multi_message_*`` 两键构造等价配置（行为零变更）。"""
        return cls(
            human_mode=False,
            ms_per_char=per_char_seconds * 1000.0,
            gap_max_ms=int(max_delay_seconds * 1000),
        )

    @classmethod
    def from_settings(cls, settings: Any) -> RhythmConfig:
        """从运行时配置构造节奏配置。

        ``human_rhythm_enabled`` 未登记时按关闭处理，保证本模块可先于配置项上线，
        且任何遗漏键都只会退回 legacy 等价行为而不会抛错。
        """
        if not bool(getattr(settings, "human_rhythm_enabled", False)):
            return cls.legacy(
                float(getattr(settings, "multi_message_delay_per_char", 0.05)),
                float(getattr(settings, "multi_message_max_delay", 2.0)),
            )
        return cls(
            human_mode=True,
            ms_per_char=float(getattr(settings, "human_typing_ms_per_char", 85)),
            gap_min_ms=int(getattr(settings, "human_gap_min_ms", 700)),
            gap_max_ms=int(getattr(settings, "human_gap_max_ms", 2600)),
            startup_min_ms=int(getattr(settings, "human_startup_min_ms", 250)),
            startup_max_ms=int(getattr(settings, "human_startup_max_ms", 900)),
            jitter_ratio=float(getattr(settings, "human_jitter_ratio", 0.2)),
            night_factor=float(getattr(settings, "human_night_factor", 1.6)),
            long_pause_prob=float(getattr(settings, "human_long_pause_prob", 0.25)),
            chars_per_frame=int(
                getattr(settings, "stream_typing_chars_per_frame", 6)
            ),
        )

    def without_randomness(self) -> RhythmConfig:
        """返回去掉抖动、长停顿与启动随机的确定性配置，供单测断言曲线形状。"""
        return replace(
            self,
            jitter_ratio=0.0,
            long_pause_prob=0.0,
            startup_min_ms=0,
            startup_max_ms=0,
        )


def pacing_delay_seconds(
    text: str, settings: Any, *, hour: int | None = None
) -> float:
    """适配器与流式闸门共用的唯一入口：返回发送该条消息前应等待的秒数。

    随机源用模块级实例（进程内共享，节奏抖动本就不该可复现）；配置每次现取，
    保证管理后台热改节奏参数后立即生效，不必重启。
    """
    cfg = RhythmConfig.from_settings(settings)
    return message_gap_ms(text, cfg, rng=_shared_rng, hour=hour) / 1000.0


def _length_curve(chars: int, cfg: RhythmConfig) -> float:
    """长句耗时曲线：越长的消息，单位字越慢（真人边打边想）。"""
    if cfg.curve_cap <= 0 or chars <= 0:
        return 1.0
    return 1.0 + min(cfg.curve_cap, chars / cfg.curve_div * cfg.curve_gain)


def _day_night_factor(hour: int | None, cfg: RhythmConfig) -> float:
    """昼夜系数：深夜更慢，非 human 档恒为 1。"""
    if hour is None or cfg.night_factor == 1.0:
        return 1.0
    if cfg.night_start_hour <= hour < cfg.night_end_hour:
        return cfg.night_factor
    return 1.0


def _energy_factor(energy: float, cfg: RhythmConfig) -> float:
    """能量系数：满能量恒为 1.0（不污染每字时长校准），零能量减速到 ``1 - energy_slowdown``。"""
    e = max(0.0, min(1.0, energy))
    return 1.0 - cfg.energy_slowdown * (1.0 - e)


def message_gap_ms(
    text: str,
    cfg: RhythmConfig,
    *,
    rng: Random,
    hour: int | None = None,
    energy: float = 1.0,
) -> int:
    """计算"发出 text 这条消息之前"应等待的毫秒数（气泡之间的停顿）。

    以**本条**文本长度计算（改动前 Bot 侧按上一条长度算，属错位一档）。
    legacy 档退化为 ``min(len * per_char, max_delay)``，与改动前逐字等价。
    """
    chars = len(text.strip()) if text else 0
    if not cfg.human_mode:
        return int(min(chars * cfg.ms_per_char, cfg.gap_max_ms))
    base = rng.uniform(cfg.startup_min_ms, cfg.startup_max_ms)
    base += chars * cfg.ms_per_char * _length_curve(chars, cfg)
    base *= _energy_factor(energy, cfg) * _day_night_factor(hour, cfg)
    if cfg.jitter_ratio > 0:
        base *= 1.0 + cfg.jitter_ratio * rng.uniform(-1.0, 1.0)
    gap = int(min(max(base, cfg.gap_min_ms), cfg.gap_max_ms))
    if cfg.long_pause_prob > 0 and rng.random() < cfg.long_pause_prob:
        gap += int(rng.uniform(cfg.long_pause_min_ms, cfg.long_pause_max_ms))
    return gap


def thinking_ms(
    cfg: RhythmConfig,
    *,
    rng: Random,
    input_chars: int = 0,
    hour: int | None = None,
) -> int:
    """首条气泡前的"已读 + 思考"延迟。

    真人看完对方消息要想一下才动手；legacy 档返回 0（不加任何延迟）。
    """
    if not cfg.human_mode:
        return 0
    base = rng.uniform(cfg.startup_min_ms, cfg.startup_max_ms)
    extra = min(cfg.think_cap_ms, max(0, input_chars) * cfg.think_per_input_char_ms)
    total = (base + extra) * _day_night_factor(hour, cfg)
    if cfg.jitter_ratio > 0:
        total *= 1.0 + cfg.jitter_ratio * rng.uniform(-1.0, 1.0)
    return int(max(0, total))


def typing_frames(
    text: str,
    cfg: RhythmConfig,
    *,
    rng: Random,
    hour: int | None = None,
    energy: float = 1.0,
) -> list[tuple[str, int]]:
    """把一段已放行的文本切成 ``(帧文本, 该帧前等待毫秒)`` 序列（逐字浮现，默认不启用）。

    整条总时长被夹在 ``frame_total_min_ms``~``frame_total_max_ms``：过短会一闪而过不像人，
    过长会让用户以为卡住。帧数由 ``chars_per_frame`` 决定，切不满一帧的尾巴单独成帧。
    """
    if not text:
        return []
    if not cfg.human_mode:
        return [(text, 0)]
    size = max(1, cfg.chars_per_frame)
    pieces = [text[i : i + size] for i in range(0, len(text), size)]
    # 目标总时长：按每字时长估，再叠长句曲线、能量与昼夜，最后夹进上下界
    target = len(text) * cfg.ms_per_char * _length_curve(len(text), cfg)
    target *= _energy_factor(energy, cfg) * _day_night_factor(hour, cfg)
    if cfg.jitter_ratio > 0:
        target *= 1.0 + cfg.jitter_ratio * rng.uniform(-1.0, 1.0)
    target = min(max(target, cfg.frame_total_min_ms), cfg.frame_total_max_ms)
    per_frame = target / len(pieces)
    return [(p, int(max(0, per_frame))) for p in pieces]
