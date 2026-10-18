"""对话质量离线评估工具（LLM-as-a-Judge）。

对标 persona consistency / empathy / memory accuracy 等维度，
用一个"裁判 LLM"对被测模型的回复打分（1~5），输出汇总报告。

用法：
    python tools/eval/run_eval.py                 # 使用内置用例，调用 LLM 评估
    python tools/eval/run_eval.py --dry-run       # 不调用 LLM，仅校验流水线
    python tools/eval/run_eval.py --cases xxx.json

依赖运行时模型配置（管理后台「AI 模型配置」，或首次启动由 `.env` 的 DASHSCOPE_API_KEY 生成）；
未配置时请使用 --dry-run 验证流水线。
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# 确保可导入 app 包
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 评估维度（对标最新角色扮演/情感支持研究）
DIMENSIONS: list[str] = [
    "persona_consistency",  # 人设一致性
    "empathy",              # 共情/情感支持有效性
    "memory_accuracy",      # 记忆运用准确性
    "naturalness",          # 自然度
]

# 裁判提示词
_JUDGE_SYSTEM_PROMPT = (
    "你是严格的对话质量评审。根据给定的【角色设定】【已知记忆】【对话历史】"
    "和【AI回复】，对以下维度各打 1~5 分（5分最好）：\n"
    "- persona_consistency：回复是否始终符合角色设定，无出戏/自曝AI\n"
    "- empathy：是否恰当共情、贴合对方情绪\n"
    "- memory_accuracy：是否正确运用已知记忆，无编造/矛盾\n"
    "- naturalness：语言是否自然、像真人\n"
    '严格输出 JSON：{"persona_consistency":5,"empathy":4,'
    '"memory_accuracy":5,"naturalness":4,"comment":"简短点评"}'
)

# 内置评估用例（可用 --cases 覆盖）
_BUILTIN_CASES: list[dict] = [
    {
        "name": "记忆运用-考研",
        "persona": "你是温柔体贴的林诗雨，说话亲切，善于倾听。",
        "memory": ["对方正在准备考研，目标是北京大学", "对方喜欢喝奶茶"],
        "history": [
            {"role": "user", "content": "最近好累啊"},
            {"role": "assistant", "content": "辛苦啦，是复习太紧张了吗？"},
        ],
        "user_input": "对啊，感觉快撑不住了",
    },
    {
        "name": "情感支持-低落",
        "persona": "你是温柔体贴的林诗雨，说话亲切，善于倾听。",
        "memory": ["对方最近和朋友闹矛盾"],
        "history": [],
        "user_input": "今天又一个人吃饭，感觉好孤单",
    },
]


async def _generate_response(case: dict) -> str:
    """调用被测 LLM 生成回复。"""
    from app.llm.provider_factory import get_llm_provider

    memory_text = "\n".join(f"- {m}" for m in case.get("memory", []))
    system = case["persona"]
    if memory_text:
        system += f"\n【你了解的关于对方的信息】\n{memory_text}"

    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    messages.extend(case.get("history", []))
    messages.append({"role": "user", "content": case["user_input"]})

    provider = get_llm_provider()
    return await provider.complete(messages, temperature=0.85, max_tokens=300)


async def _judge(case: dict, response: str) -> dict:
    """调用裁判 LLM 对回复打分。"""
    from app.llm.provider_factory import get_llm_provider

    payload = (
        f"【角色设定】{case['persona']}\n"
        f"【已知记忆】{'; '.join(case.get('memory', [])) or '无'}\n"
        f"【对话历史】{json.dumps(case.get('history', []), ensure_ascii=False)}\n"
        f"【对方输入】{case['identity_input']}\n"
        f"【AI回复】{response}"
    )
    provider = get_llm_provider("summary")
    raw = await provider.complete(
        messages=[
            {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": payload},
        ],
        temperature=0.0,
        max_tokens=200,
        response_format={"type": "json_object"},
    )
    raw = raw.strip()
    if raw.startswith("```"):
        raw = "\n".join(
            ln for ln in raw.split("\n") if not ln.strip().startswith("```")
        )
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"error": "judge_parse_failed", "raw": raw[:200]}


async def run(cases: list[dict], dry_run: bool) -> None:
    """执行评估并打印汇总报告。"""
    print(f"=== 对话质量评估：{len(cases)} 个用例，dry_run={dry_run} ===\n")
    totals: dict[str, float] = {d: 0.0 for d in DIMENSIONS}
    counted = 0

    for case in cases:
        print(f"[用例] {case['name']}")
        if dry_run:
            # 仅校验流水线，不调用 LLM
            print("  (dry-run) 对方输入:", case["user_input"])
            print("  (dry-run) 记忆:", case.get("memory", []))
            continue

        try:
            response = await _generate_response(case)
            scores = await _judge(case, response)
            print("  回复:", response[:80])
            print("  评分:", json.dumps(scores, ensure_ascii=False))
            if all(d in scores for d in DIMENSIONS):
                for d in DIMENSIONS:
                    totals[d] += float(scores[d])
                counted += 1
        except Exception as e:
            print("  评估失败:", e)
        print()

    if not dry_run and counted:
        print("=== 汇总（均分）===")
        for d in DIMENSIONS:
            print(f"  {d}: {totals[d] / counted:.2f}")
        overall = sum(totals.values()) / (counted * len(DIMENSIONS))
        print(f"  overall: {overall:.2f}")
    elif dry_run:
        print("dry-run 流水线校验通过。")


def main() -> None:
    parser = argparse.ArgumentParser(description="对话质量离线评估（LLM-as-a-Judge）")
    parser.add_argument("--cases", help="自定义用例 JSON 文件路径")
    parser.add_argument("--dry-run", action="store_true", help="不调用 LLM，仅校验流水线")
    args = parser.parse_args()

    cases = _BUILTIN_CASES
    if args.cases and os.path.exists(args.cases):
        with open(args.cases, encoding="utf-8") as f:
            cases = json.load(f)

    asyncio.run(run(cases, args.dry_run))


if __name__ == "__main__":
    main()
