"""记忆治理与反思层的确定性尺子（零模型调用、零噪声）。

为什么不用 GSB：反思层的收益是跨天跨会话的，而 run_eval 是 13 例单轮、
每例独立组装（不复用会话、不写库），结构上量不到"长期越来越懂我"；
再叠加 n=13 的裁判噪声 ±15%，用主观分判定这轮改动等于自欺。
所以这里只量**可证伪的卫生与溯源事实**：

  · 脏指称条数——角色记忆里还留着多少"用户…""…与AI…"这类出戏写法
  · 点状陈旧——多少条"未来安排"已经过期却仍在候选里
  · 完全重复 / 改述重复——同一件事被记了几遍
  · 洞见溯源完整率——每条洞见是否真能点出 ≥2 条仍然存在的来源记忆
    （grounding 是本机制唯一的反幻觉护栏，必须常驻监控而不是一次性验收）

用法（在 app 容器内执行；tools/ 不在镜像里，需先 docker cp）：
    docker exec -i -w /app aiyichat-app-1 python /app/tools/eval/memory_hygiene_stats.py
"""

import asyncio
import os
import sys
from datetime import datetime, timezone

# 按文件路径执行时 sys.path[0] 是脚本所在目录（tools/eval），需回到项目根
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)


async def collect() -> dict[str, object]:
    from app.config import settings
    from app.conversation.memory_hygiene import audit_memory, is_restatement
    from app.db.mongo import close_db, connect_db, get_database
    from app.models.service_memory import ServiceMemoryInDB

    await connect_db()
    await settings.reload()
    db = get_database()
    docs = await db.service_memories.find(
        {}, {"service_id": 1, "facts": 1, "insights": 1}
    ).to_list(None)

    now = datetime.now(timezone.utc)
    totals = {
        "services": len(docs),
        "facts": 0,
        "insights": 0,
        "dirty_referent": 0,
        "punctual_stale": 0,
        "exact_duplicate": 0,
        "restatement_pairs": 0,
        "insight_untraceable": 0,
        "insight_thin_evidence": 0,
    }
    samples: list[str] = []

    for doc in docs:
        # 走 from_mongo 而非 model_validate：fid / tier / punctual 的存量补齐都在那里
        record = ServiceMemoryInDB.from_mongo({**doc, "_id": str(doc["_id"])})
        facts, insights = list(record.facts), list(record.insights)
        report = audit_memory(facts, now)
        totals["facts"] += len(facts)
        totals["insights"] += len(insights)
        totals["dirty_referent"] += int(str(report["dirty_referent"]))
        totals["punctual_stale"] += int(str(report["punctual_stale"]))
        totals["exact_duplicate"] += int(str(report["exact_duplicate"]))
        for s in (report["dirty_samples"] or []):
            samples.append(f"脏指称: {s}")
        for s in (report["stale_samples"] or []):
            samples.append(f"陈旧点状: {s}")

        # 改述重复：两两比对（单服务事实数受 memory_max_facts 约束，代价可接受）
        for i in range(len(facts)):
            for j in range(i + 1, len(facts)):
                if is_restatement(facts[i].content, facts[j].content):
                    totals["restatement_pairs"] += 1
                    samples.append(
                        f"改述重复: {facts[i].content[:20]} || {facts[j].content[:20]}"
                    )

        # 洞见溯源：来源 fid 必须能在本服务事实里找到，且不少于门槛条数
        live_fids = {f.fid for f in facts if f.fid}
        for ins in insights:
            matched = [fid for fid in ins.source_fids if fid in live_fids]
            if len(matched) < settings.reflection_min_evidence:
                totals["insight_thin_evidence"] += 1
                samples.append(f"洞见来源不足({len(matched)}): {ins.content[:24]}")
                if not matched:
                    totals["insight_untraceable"] += 1

    await close_db()
    return {"totals": totals, "samples": samples[:20]}


def main() -> int:
    result = asyncio.run(collect())
    totals: dict[str, object] = result["totals"]  # type: ignore[assignment]
    samples: list[str] = result["samples"]  # type: ignore[assignment]
    print("=== 记忆卫生与洞见溯源（确定性指标）===")
    for key, value in totals.items():
        print(f"  {key:<22} {value}")
    print("\n--- 样本（最多 20 条）---")
    for line in samples:
        print(f"  · {line}")
    # 判定：这些计数应当随治理推进单调收敛到 0
    bad = int(str(totals["dirty_referent"])) + int(str(totals["insight_thin_evidence"]))
    print(f"\n结论: {'存在待治理项' if bad else '脏指称与溯源缺口均为 0'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
