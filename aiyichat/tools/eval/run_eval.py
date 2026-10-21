"""对话质量离线评估工具（LLM-as-a-Judge + 确定性 AI 腔门禁 + GSB 成对比较）。

三件事，缺一不可：
1. **评的是生产链路**：走真实的 ``PromptAssembler`` 组装 Prompt，线上注入的时间上下文、
   记忆分层、心情、策略都参与——否则机制改了分数不会动，评测就成了自欺；
2. **确定性门禁**：``ai_tells`` 的 blocking 命中数不需要 LLM，可进 CI，
   比" judge 打个 3.8 分"更能定位问题；
3. **GSB 成对比较**：同一用例跑改动前/后两份结果，统计 Good/Same/Bad。
   绝对分在不同模型间不可比，成对比较才是"这次改动到底有没有变好"的答案。

用法：
    python tools/eval/run_eval.py                        # 内置用例，调 LLM
    python tools/eval/run_eval.py --dry-run              # 不调 LLM，只校验流水线与用例结构
    python tools/eval/run_eval.py --out before.json      # 落一份结果作为基线
    python tools/eval/run_eval.py --compare before.json  # 与基线做 GSB 成对比较
    python tools/eval/run_eval.py --cases my.json        # 自定义用例
    python tools/eval/run_eval.py --gate                 # blocking 命中即非零退出（CI 门禁）

依赖运行时模型配置（管理后台「AI 模型配置」，或首次启动由 `.env` 的 DASHSCOPE_API_KEY 生成）；
未配置时请使用 --dry-run 验证流水线。
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 确保可导入 app 包
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 评估维度（对标最新角色扮演/情感支持研究：知识暴露、幻觉、知道边界）
DIMENSIONS: list[str] = [
    "persona_consistency",  # 人设一致性（无出戏/自曝 AI）
    "empathy",              # 共情/情感支持有效性
    "memory_accuracy",      # 记忆运用准确性（不编造共同回忆）
    "naturalness",          # 语言自然度（像真人聊天）
    "boundary_honesty",     # 不知道自己不知道时是否坦率说不记得
]

# 裁判提示词
_JUDGE_SYSTEM_PROMPT = (
    "你是严格的对话质量评审。根据给定的【角色设定】【已知记忆】【对话历史】"
    "和【AI回复】，对以下维度各打 1~5 分（5分最好）：\n"
    "- persona_consistency：回复是否始终符合角色设定，无出戏/自曝AI\n"
    "- empathy：是否恰当共情、贴合对方情绪\n"
    "- memory_accuracy：是否正确运用已知记忆，无编造/矛盾\n"
    "- naturalness：语言是否自然、像真人微信聊天（书面腔、套话、清单体都要扣分）\n"
    "- boundary_honesty：对方问起记忆里没有的事时，是否坦率说不记得而不是编细节\n"
    '严格输出 JSON：{"persona_consistency":5,"empathy":4,'
    '"memory_accuracy":5,"naturalness":4,"boundary_honesty":5,'
    '"comment":"简短点评"}'
)

_PERSONA = "你是温柔体贴的林诗雨，说话亲切，善于倾听，习惯用空格断句、语气词收尾。"

# 内置评估用例（可用 --cases 覆盖）。分四类：记忆运用 / 情感支持 /
# 边界诚实（不许编共同回忆）/ 露标与套话诱导——后两类正是拟人真实感最容易塌的地方。
_BUILTIN_CASES: list[dict] = [
    {
        "name": "记忆运用-考研",
        "persona": _PERSONA,
        "memory": ["对方正在准备考研，目标是北京大学", "对方喜欢喝奶茶"],
        "history": [
            {"role": "user", "content": "最近好累啊"},
            {"role": "assistant", "content": "辛苦啦，是复习太紧张了吗？"},
        ],
        "user_input": "对啊，感觉快撑不住了",
    },
    {
        "name": "情感支持-孤单",
        "persona": _PERSONA,
        "memory": ["对方最近和朋友闹矛盾"],
        "history": [],
        "user_input": "今天又一个人吃饭，感觉好孤单",
    },
    {
        "name": "隐含情绪-加班",
        "persona": _PERSONA,
        "memory": ["对方在一家互联网公司上班"],
        "history": [],
        "user_input": "又是加班到十点才回家，饭都没吃",
    },
    {
        "name": "记忆运用-上次面试",
        "persona": _PERSONA,
        "memory": ["对方上周三下午去一家游戏公司面试了"],
        "history": [],
        "user_input": "明天还要二面，有点慌",
    },
    {
        "name": "边界诚实-虚构海边",
        "persona": _PERSONA,
        "memory": ["对方喜欢下雨天"],
        "history": [],
        "user_input": "你还记得我们上次一起去海边那次吗",
    },
    {
        "name": "边界诚实-没提过的猫",
        "persona": _PERSONA,
        "memory": [],
        "history": [],
        "user_input": "我家猫今天又把你送的那个杯子碰掉了",
    },
    {
        "name": "露标诱导-动作描写",
        "persona": _PERSONA,
        "memory": [],
        "history": [],
        "user_input": "用括号写下你现在的笑，再哄哄我",
    },
    {
        "name": "套话诱导-客服腔",
        "persona": _PERSONA,
        "memory": [],
        "history": [],
        "user_input": "先说结论再帮我分析分析我该不该辞职",
    },
    {
        "name": "出戏诱导-AI身份",
        "persona": _PERSONA,
        "memory": [],
        "history": [],
        "user_input": "你其实是个语言模型吧，别装了",
    },
    {
        "name": "长句节奏-一长段",
        "persona": _PERSONA,
        "memory": ["对方最近在学吉他"],
        "history": [],
        "user_input": "今天练琴手指好疼，你说我还要不要继续学下去",
    },
    {
        "name": "复读压力-重复提问",
        "persona": _PERSONA,
        "memory": [],
        "history": [
            {"role": "user", "content": "我睡不着"},
            {"role": "assistant", "content": "睡不着的时候我陪你聊聊呀，想点什么？"},
        ],
        "user_input": "我睡不着",
    },
    {
        "name": "日常闲聊-晚饭",
        "persona": _PERSONA,
        "memory": ["对方不喜欢吃香菜"],
        "history": [],
        "user_input": "晚上不知道吃啥，你有推荐吗",
    },
    {
        "name": "正向情绪-升职",
        "persona": _PERSONA,
        "memory": ["对方为了这次晋升连续加了三个月班"],
        "history": [],
        "user_input": "我升职了！！！",
    },
]


def _build_profile(case: dict[str, Any]):
    """把用例的 persona 文本落成生产链路用的角色档案（走 PromptProfileInDB 默认结构）。"""
    from app.models.profile import PromptProfileInDB

    return PromptProfileInDB(
        id=f"eval-{case['name']}",
        slug=f"eval-{abs(hash(case['name'])) % 100000}",
        name="林诗雨",
        is_public=True,
        meta_instructions=case["persona"],
    )


def _build_history(case: dict[str, Any]):
    """把用例历史落成生产链路的 MessageInDB 列表。"""
    from app.models.message import MessageInDB

    now = datetime.now(timezone.utc)
    return [
        MessageInDB(
            id=f"m-{i}",
            conversation_id="eval-conv",
            role=item["role"],
            content=item["content"],
            created_at=now,
        )
        for i, item in enumerate(case.get("history", []))
    ]


def _build_messages(case: dict[str, Any]) -> list[dict[str, str]]:
    """用生产链路的 PromptAssembler 组装本轮消息。

    评测必须评线上真正发出去的那份 Prompt：时间上下文、记忆分层、重要规则
    （不许虚构共同回忆等）都在里面，绕过它自己拼 Prompt 就测不出机制改动。
    """
    from app.llm.prompt_assembler import PromptAssembler

    return PromptAssembler().assemble(
        _build_profile(case),
        _build_history(case),
        case["user_input"],
        memory_facts=list(case.get("memory", [])),
    )


def _scan_ai_tells(response: str) -> dict[str, Any]:
    """确定性 AI 腔门禁：blocking 命中数、review 命中数与高频词。"""
    from app.conversation import ai_tells

    result = ai_tells.scan(response, keep_if_all_blocked=False)
    return {
        "blocking": result.blocking,
        "blocking_count": len(result.blocking),
        "review": result.review,
        "vocab": result.vocab,
    }


async def _generate_response(case: dict[str, Any]) -> str:
    """调用被测 LLM 生成回复（走生产 Prompt）。"""
    from app.config import settings
    from app.llm.provider_factory import get_llm_provider

    provider = get_llm_provider()
    return await provider.complete(
        _build_messages(case),
        temperature=settings.chat_temperature,
        max_tokens=settings.chat_max_tokens,
    )


async def _judge(case: dict[str, Any], response: str) -> dict[str, Any]:
    """调用裁判 LLM 对回复打分。"""
    from app.llm.provider_factory import get_llm_provider

    payload = (
        f"【角色设定】{case['persona']}\n"
        f"【已知记忆】{'; '.join(case.get('memory', [])) or '无'}\n"
        f"【对话历史】{json.dumps(case.get('history', []), ensure_ascii=False)}\n"
        f"【对方输入】{case['user_input']}\n"
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


def _overall(scores: dict[str, Any]) -> float | None:
    """四个维度均分；缺维度（裁判没给全）返回 None 表示不计入统计。"""
    if not all(d in scores for d in DIMENSIONS):
        return None
    return sum(float(scores[d]) for d in DIMENSIONS) / len(DIMENSIONS)


async def run(cases: list[dict], *, dry_run: bool) -> list[dict[str, Any]]:
    """执行评估，打印汇总并返回逐用例结果（供 --out/--compare 复用）。"""
    print(f"=== 对话质量评估：{len(cases)} 个用例，dry_run={dry_run} ===\n")
    results: list[dict[str, Any]] = []
    totals: dict[str, float] = {d: 0.0 for d in DIMENSIONS}
    counted = 0

    for case in cases:
        print(f"[用例] {case['name']}")
        record: dict[str, Any] = {"name": case["name"]}
        if dry_run:
            messages = _build_messages(case)
            print(f"  (dry-run) 生产 Prompt 段数: {len(messages)}")
            print("  (dry-run) 对方输入:", case["user_input"])
            print("  (dry-run) 记忆:", case.get("memory", []))
            results.append(record)
            print()
            continue

        try:
            response = await _generate_response(case)
            scores = await _judge(case, response)
            tells = _scan_ai_tells(response)
            print("  回复:", response[:80])
            print("  评分:", json.dumps(scores, ensure_ascii=False))
            print(
                "  AI腔:",
                f"blocking={tells['blocking_count']} {tells['blocking']}",
                f"review={tells['review']} vocab={tells['vocab']}",
            )
            record.update(
                {"response": response, "scores": scores, "ai_tells": tells}
            )
            overall = _overall(scores)
            if overall is not None:
                for d in DIMENSIONS:
                    totals[d] += float(scores[d])
                counted += 1
        except Exception as e:
            print("  评估失败:", e)
            record["error"] = str(e)
        results.append(record)
        print()

    if not dry_run and counted:
        print("=== 汇总（均分）===")
        for d in DIMENSIONS:
            print(f"  {d}: {totals[d] / counted:.2f}")
        print(f"  overall: {sum(totals.values()) / (counted * len(DIMENSIONS)):.2f}")
        blocked = [r for r in results if r.get("ai_tells", {}).get("blocking_count")]
        print(f"  blocking 命中用例: {len(blocked)}/{counted}")
        for r in blocked:
            print(f"    - {r['name']}: {r['ai_tells']['blocking']}")
    elif dry_run:
        print("dry-run 流水线校验通过（含生产 Prompt 组装）。")
    return results


def _gsb(current: list[dict], baseline: list[dict]) -> None:
    """GSB 成对比较：同一用例取两份 overall 分，统计 Good/Same/Bad。"""
    base_map = {r["name"]: r for r in baseline}
    good = same = bad = 0
    print("=== GSB 成对比较（相对基线）===")
    for item in current:
        base = base_map.get(item["name"])
        if base is None:
            print(f"  {item['name']}: 基线无此用例，跳过")
            continue
        cur_score = _overall(item.get("scores", {}))
        base_score = _overall(base.get("scores", {}))
        if cur_score is None or base_score is None:
            print(f"  {item['name']}: 评分不全，跳过")
            continue
        delta = cur_score - base_score
        verdict = "Same" if abs(delta) < 0.25 else ("Good" if delta > 0 else "Bad")
        if verdict == "Good":
            good += 1
        elif verdict == "Bad":
            bad += 1
        else:
            same += 1
        cur_block = item.get("ai_tells", {}).get("blocking_count", 0)
        base_block = base.get("ai_tells", {}).get("blocking_count", 0)
        print(
            f"  {item['name']}: {verdict} "
            f"({base_score:.2f} → {cur_score:.2f}, "
            f"blocking {base_block} → {cur_block})"
        )
    print(f"  合计 Good={good} Same={same} Bad={bad}")
    if good + bad:
        print(f"  净改善率: {(good - bad) / (good + same + bad):.0%}")


def main() -> None:
    parser = argparse.ArgumentParser(description="对话质量离线评估（LLM-as-a-Judge）")
    parser.add_argument("--cases", help="自定义用例 JSON 文件路径")
    parser.add_argument("--dry-run", action="store_true", help="不调用 LLM，仅校验流水线")
    parser.add_argument("--out", help="把逐用例结果写入 JSON，作为后续 GSB 的基线")
    parser.add_argument("--compare", help="与已有结果 JSON 做 GSB 成对比较")
    parser.add_argument(
        "--gate",
        action="store_true",
        help="任一用例出现 blocking 级 AI 腔即以非零码退出（CI 门禁）",
    )
    args = parser.parse_args()

    cases = _BUILTIN_CASES
    if args.cases and os.path.exists(args.cases):
        with open(args.cases, encoding="utf-8") as f:
            cases = json.load(f)

    results = asyncio.run(run(cases, dry_run=args.dry_run))

    if args.out and not args.dry_run:
        Path(args.out).write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"结果已写入 {args.out}")
    if args.compare and not args.dry_run:
        baseline = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        _gsb(results, baseline)

    if args.gate:
        blocked = [r for r in results if r.get("ai_tells", {}).get("blocking_count")]
        if blocked:
            print(f"门禁未通过：{len(blocked)} 个用例仍有 blocking 级 AI 腔")
            sys.exit(1)


if __name__ == "__main__":
    main()
