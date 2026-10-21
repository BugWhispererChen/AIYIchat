"""真实流量长度统计（G35 的尺子之二）。

``run_eval.py`` 量的是用例上的回复形态，本脚本量**线上真实聊出来的东西**：
用户说了多少字、角色回了多少字、一轮砸出几条气泡。改动前后各跑一次，
中位数与比值就是"角色是不是终于像人一样短"的直接证据。

用法（在测试服容器内运行，tools/ 不在镜像里，需先 docker cp）：
    docker cp tools/eval/length_stats.py aiyichat-app-1:/app/tools/eval/
    docker exec aiyichat-app-1 python tools/eval/length_stats.py --days 0
    docker exec aiyichat-app-1 python tools/eval/length_stats.py --days 30 --json /tmp/len.json
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 长度分桶（字）：真人 IM 的主体落在前两桶，角色却大量落在后两桶就是失控
_BUCKETS: list[tuple[str, int]] = [
    ("<=10", 10),
    ("11-25", 25),
    ("26-50", 50),
    ("51-100", 100),
    ("101-200", 200),
]
_BUCKET_NAMES = [name for name, _ in _BUCKETS] + [">200"]


def _percentile(values: list[int], pct: float) -> int:
    """最近秩百分位（样本量小时够用，本项目只有几百条）。"""
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * pct))]


def _summary(values: list[int]) -> dict[str, Any]:
    """一组长度的均值/分位/最大值。"""
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "mean": round(sum(values) / len(values), 1),
        "p25": _percentile(values, 0.25),
        "p50": _percentile(values, 0.50),
        "p75": _percentile(values, 0.75),
        "p90": _percentile(values, 0.90),
        "max": max(values),
    }


def _buckets(values: list[int]) -> dict[str, float]:
    """各长度桶的占比（%），用于看分布形状而不只是均值。"""
    if not values:
        return {}
    counts = dict.fromkeys(_BUCKET_NAMES, 0)
    for value in values:
        for name, upper in _BUCKETS:
            if value <= upper:
                counts[name] += 1
                break
        else:
            counts[">200"] += 1
    return {name: round(100 * count / len(values), 1) for name, count in counts.items()}


def _segments(content: str) -> list[str]:
    """一条落库回复拆出的气泡数（引擎用换行拼接多条拆分结果）。"""
    return [part.strip() for part in (content or "").split("\n") if part.strip()]


def collect(docs: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """把消息文档汇总成长度指标。"""
    user_lens: list[int] = []
    bot_lens: list[int] = []
    bubble_counts: list[int] = []
    bubble_lens: list[int] = []
    for doc in docs:
        content = (doc.get("content") or "").strip()
        if not content:
            continue
        if doc.get("role") == "user":
            user_lens.append(len(content))
        elif doc.get("role") == "assistant":
            bot_lens.append(len(content))
            parts = _segments(content)
            bubble_counts.append(len(parts))
            bubble_lens.extend(len(p) for p in parts)
    user_median = _percentile(user_lens, 0.50) or 1
    return {
        "user": {**_summary(user_lens), "buckets": _buckets(user_lens)},
        "assistant": {**_summary(bot_lens), "buckets": _buckets(bot_lens)},
        "bubbles_per_turn": _summary(bubble_counts),
        "bubble_chars": _summary(bubble_lens),
        "length_ratio_p50": round(_percentile(bot_lens, 0.50) / user_median, 2),
        "sample": {"user": len(user_lens), "assistant": len(bot_lens)},
    }


def print_report(report: dict[str, Any], *, since: datetime | None) -> None:
    """打印可读报告，并对照验收目标线提示是否达标。"""
    scope = f"最近 {since.date()} 起" if since else "全部历史"
    print(f"=== 真实流量长度统计（{scope}）===")
    for key in ("user", "assistant"):
        part = report[key]
        print(
            f"  {key:9s} n={part['n']:4d} 中位={part.get('p50', 0):4d} "
            f"p75={part.get('p75', 0):4d} p90={part.get('p90', 0):4d} "
            f"最长={part.get('max', 0):4d}"
        )
    bubbles = report["bubbles_per_turn"]
    bubble_chars = report["bubble_chars"]
    print(
        f"  一轮气泡   中位={bubbles.get('p50', 0)} 最多={bubbles.get('max', 0)}"
        f"；单气泡中位={bubble_chars.get('p50', 0)} 字"
    )
    print(f"  回复/对方长度比（中位）：{report['length_ratio_p50']}x")
    print("  分布：")
    for key in ("user", "assistant"):
        print(f"    {key:9s} {report[key]['buckets']}")
    verdict = _verdict(report)
    print("  目标线（整轮 ≤55 字 / ≤3 条 / 比值 ≤2.5x）：" + verdict)


def _verdict(report: dict[str, Any]) -> str:
    """对照 G35 验收线给出简短期望：任一项超标就报哪一项。"""
    targets = [
        ("整轮中位", report["assistant"].get("p50", 0), 55),
        ("一轮气泡中位", report["bubbles_per_turn"].get("p50", 0), 3),
        ("长度比", report["length_ratio_p50"], 2.5),
    ]
    over = [f"{name} {value}>{limit}" for name, value, limit in targets if value > limit]
    return "全部达标" if not over else "未达标：" + "、".join(over)


async def _run(days: int, json_path: str | None) -> int:
    """连接 DB（motor 异步客户端）、取数、打印报告（可选导出 JSON 供前后对照）。"""
    from app.db import mongo

    await mongo.connect_db()
    collection = mongo.get_database().messages
    query: dict[str, Any] = {"content_type": "text"}
    since: datetime | None = None
    if days > 0:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        query["created_at"] = {"$gte": since}
    docs = await collection.find(query, {"role": 1, "content": 1}).to_list(20000)
    report = collect(docs)
    print_report(report, since=since)
    if json_path:
        Path(json_path).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"  已写出 {json_path}")
    await mongo.close_db()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="真实流量回复长度统计")
    parser.add_argument("--days", type=int, default=0, help="只看最近 N 天，0=全部")
    parser.add_argument("--json", help="把指标写入 JSON 文件，便于改前后 diff")
    args = parser.parse_args()
    return asyncio.run(_run(args.days, args.json))


if __name__ == "__main__":
    sys.exit(main())
