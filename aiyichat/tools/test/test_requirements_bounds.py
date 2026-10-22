"""依赖钉版护栏：dashscope / kuzu 必须同时有下界与上界。

`mypy app` 在本地全绿、在 CI 报 30 处真错，根因就是这两个包只写 `>=`：
本地 venv 装的是没有 `py.typed` 的旧版（全 `Any`），CI 全新安装拿到带存根的新版
（tts/asr/imagegen 调 dashscope、entity_graph 调 kuzu 的 dict 展开与 union 属性
当场暴露）。钉上界让两端口径锁死；一旦有人为了升依赖把上界删掉，本用例先红，
逼着他同步复跑 mypy 而不是让 CI 莫名其妙变红。
"""

import re
from pathlib import Path

_REQUIREMENTS = Path(__file__).resolve().parents[2] / "requirements.txt"

# 需要同时具备 `>=X,<Y` 两条边界的包：都是自带类型存根、会改变 mypy 结论的第三方 SDK
_BOUND_REQUIREMENTS = ("dashscope", "kuzu")

# 形如 `dashscope>=1.26.4,<1.27`（允许包名后有空格与可选 extras）
_LINE_RE = re.compile(r"^(?P<name>[A-Za-z0-9_.\-]+)\s*(?P<spec>[^#]*)$")


def _specs() -> dict[str, str]:
    """解析 requirements.txt，返回 `包名 → 版本约束串`。"""
    specs: dict[str, str] = {}
    for raw in _REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        matched = _LINE_RE.match(line)
        if matched:
            specs[matched.group("name").lower()] = matched.group("spec").strip()
    return specs


def test_stub_bearing_sdks_are_upper_bound() -> None:
    """dashscope/kuzu 必须同时钉下界与上界。"""
    specs = _specs()
    for name in _BOUND_REQUIREMENTS:
        assert name in specs, f"requirements.txt 里找不到 {name}"
        spec = specs[name]
        assert ">=" in spec, f"{name} 缺少下界：{spec}"
        assert "<" in spec, (
            f"{name} 缺少上界（当前 {spec}）：该包自带类型存根，"
            f"不钉上界会让本地与 CI 的 mypy 口径随上游版本漂移"
        )
