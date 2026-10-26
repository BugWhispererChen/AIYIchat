"""聊天记录克隆专用离线评测。

与 run_eval.py 完全独立：不改既有五维 DIMENSIONS，不使历史基线失效。
输入按时间留出的角色/回放用例，对同一上下文分别生成：
  baseline = 现有 Profile-only
  clone    = Profile + 确定性风格提示 + 情境对话对
输出盲化成对风格胜负、确定性长度/表情差、隐私泄漏和 bootstrap 置信区间。

用法：
  python tools/eval/run_clone_eval.py --cases tools/eval/clone_cases.example.json --dry-run
  python tools/eval/run_clone_eval.py --cases cases.json --out clone-result.json --gate
"""

import argparse
import asyncio
import hashlib
import json
import random
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF]")
_PARTICLES = set("呀哇哈嘞喽咯哟啦嘛呢吧啊哦嗯唉诶嘿")

_JUDGE_PROMPT = """你是严格的角色模仿盲评员。你会看到目标本人的风格证据、当前情境、真实回复，
以及两份匿名候选回复 A/B。请判断哪一份在“表达方式”上更像目标本人在这个情境下会说的话。

只评表达，不评谁更礼貌、更正确或信息更多。允许平局。
输出严格 JSON：
{"style_winner":"A|B|tie","confidence":1-5,
 "reason":"不超过50字，指出句长/语气/承接方式的具体差异",
 "fact_leak":"A|B|none","fact_leak_note":"候选是否错误复述参考中的事实或关系，无则空"}
"""


def _bootstrap_ci(wins: list[int], *, rounds: int = 1000) -> tuple[float, float]:
    """净胜值(胜=1/平=0/负=-1)的 95% bootstrap 区间。"""
    if not wins:
        return 0.0, 0.0
    rng = random.Random(20261025)
    values: list[float] = []
    for _ in range(rounds):
        sample = [wins[rng.randrange(len(wins))] for _ in wins]
        values.append(sum(sample) / len(sample))
    values.sort()
    return values[int(0.025 * rounds)], values[int(0.975 * rounds) - 1]


def _surface_metrics(reply: str, reference: str) -> dict[str, Any]:
    rep = (reply or "").strip()
    ref = (reference or "").strip()
    rep_emoji = len(_EMOJI_RE.findall(rep))
    ref_emoji = len(_EMOJI_RE.findall(ref))
    rep_particles = sum(ch in _PARTICLES for ch in rep[-8:])
    ref_particles = sum(ch in _PARTICLES for ch in ref[-8:])
    return {
        "reply_chars": len(rep),
        "reference_chars": len(ref),
        "char_gap": abs(len(rep) - len(ref)),
        "reply_emoji": rep_emoji,
        "reference_emoji": ref_emoji,
        "emoji_gap": abs(rep_emoji - ref_emoji),
        "reply_ending_particles": rep_particles,
        "reference_ending_particles": ref_particles,
        "particle_gap": abs(rep_particles - ref_particles),
    }


def _leaked_spans(reply: str, spans: list[str]) -> list[str]:
    text = (reply or "").replace(" ", "")
    return [s for s in spans if s and s.replace(" ", "") in text]


def _as_chat_messages(role: dict) -> list:
    from app.chat_import.models import ChatMessage

    result = []
    for idx, item in enumerate(role.get("train_messages", [])):
        raw_ts = item.get("timestamp")
        if isinstance(raw_ts, str):
            try:
                ts = datetime.fromisoformat(raw_ts)
            except ValueError:
                ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        else:
            ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        result.append(ChatMessage(
            timestamp=ts,
            sender=str(item.get("sender", "对方")),
            content=str(item.get("content", "")),
            is_target=bool(item.get("is_target", False)),
            source_index=idx,
            message_type=str(item.get("message_type", "text")),
        ))
    return result


def _build_profile(role: dict):
    from app.models.profile import PromptProfileInDB

    payload = dict(role.get("profile", {}))
    payload.setdefault("slug", f"clone-eval-{role['name']}")
    payload.setdefault("name", role["name"])
    return PromptProfileInDB(_id=f"eval-{role['name']}", **payload)


def _case_messages(history: list[dict]):
    from app.models.message import MessageInDB

    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    return [
        MessageInDB(
            id=f"h-{i}", conversation_id="clone-eval", role=m["role"],
            content=m["content"], created_at=now,
        )
        for i, m in enumerate(history)
    ]


def _render_clone_hint(drafts, selected) -> str:
    from app.models.clone import CloneEpisodeInDB
    from app.services.clone_service import CloneContext, render_clone_context

    examples = [
        CloneEpisodeInDB(
            dataset_id="eval", started_at=d.started_at, ended_at=d.ended_at,
            context_text=d.context_text, target_text=d.target_text,
            scene=d.scene, quality=d.quality,
        )
        for d, _ in selected
    ]
    return render_clone_context(
        CloneContext(examples=examples, scores=[s for _, s in selected])
    )


async def _generate(messages: list[dict[str, str]]) -> str:
    from app.config import settings
    from app.llm.provider_factory import get_llm_provider

    return await get_llm_provider().complete(
        messages,
        temperature=settings.chat_temperature,
        max_tokens=settings.chat_max_tokens,
    )


async def _judge(
    *, style_hint: str, context: str, reference: str, a: str, b: str,
) -> dict[str, Any]:
    from app.llm.provider_factory import get_llm_provider

    payload = (
        f"【目标风格统计】{style_hint}\n"
        f"【当前情境】{context}\n"
        f"【目标本人的真实回复（仅作风格参照，不要求内容相同）】{reference}\n"
        f"【候选 A】{a}\n【候选 B】{b}"
    )
    raw = await get_llm_provider("summary").complete(
        messages=[
            {"role": "system", "content": _JUDGE_PROMPT},
            {"role": "user", "content": payload},
        ],
        temperature=0.0, max_tokens=300,
        response_format={"type": "json_object"},
    )
    raw = raw.strip()
    if raw.startswith("```"):
        raw = "\n".join(
            line for line in raw.split("\n") if not line.startswith("```")
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"error": "judge_parse_failed", "raw": raw[:200]}
    if parsed.get("style_winner") not in {"A", "B", "tie"}:
        parsed["error"] = "invalid_winner"
    return parsed


async def _bootstrap() -> None:
    from app.config import settings
    from app.db import mongo

    await mongo.connect_db()
    await settings.reload()


async def run(cases_doc: dict, *, dry_run: bool, allow_non_test: bool) -> dict:
    from app.chat_import.clone_corpus import (
        build_episodes,
        compute_style_profile,
        render_style_hint,
        select_runtime_episodes,
    )
    from app.llm.prompt_assembler import PromptAssembler
    from app.services.clone_service import rank_episode_drafts_lexical

    if not dry_run:
        try:
            await _bootstrap()
        except Exception as e:
            print("!! 运行时配置加载失败，将使用代码默认值:", e)

    roles = cases_doc.get("roles", [])
    if not roles:
        raise ValueError("用例文件根对象缺少 roles 数组")
    assembler = PromptAssembler()
    records: list[dict] = []
    net_wins: list[int] = []
    leak_count = 0

    for role in roles:
        name = str(role.get("name", "未命名"))
        messages = _as_chat_messages(role)
        drafts = build_episodes(messages, name)
        selected_library = select_runtime_episodes(drafts)
        style = compute_style_profile(messages, drafts)
        style_hint = render_style_hint(style)
        base_profile = _build_profile(role)
        clone_profile = base_profile.model_copy(update={
            "clone_style_hint": style_hint,
        })
        base_profile = base_profile.model_copy(update={"clone_style_hint": ""})

        test_cases = role.get("test_cases", [])
        if not test_cases:
            raise ValueError(f"角色 {name} 没有 test_cases")
        for case in test_cases:
            split = case.get("split", "test")
            if not allow_non_test and split != "test":
                raise ValueError(f"角色 {name} 含非 test 用例 {case.get('name')}")
            history = _case_messages(case.get("history", []))
            user_input = str(case["user_input"])
            reference = str(case.get("reference", ""))
            selected = rank_episode_drafts_lexical(
                selected_library,
                text=user_input,
                history=history,
                top_k=2,
                min_score=0.0,
            )
            clone_hint = _render_clone_hint(drafts, selected)
            base_messages = assembler.assemble(
                profile=base_profile, history=history, identity_input=user_input,
            )
            clone_messages = assembler.assemble(
                profile=clone_profile, history=history, identity_input=user_input,
                clone_hint=clone_hint,
            )
            record: dict[str, Any] = {
                "role": name,
                "case": case.get("name", user_input[:20]),
                "retrieved": [
                    {"scene": d.scene, "score": round(s, 4)} for d, s in selected
                ],
                "style_hint": style_hint,
            }
            if dry_run:
                record.update({
                    "baseline_prompt_tokens": sum(len(m["content"]) for m in base_messages),
                    "clone_prompt_tokens": sum(len(m["content"]) for m in clone_messages),
                    "clone_hint": clone_hint,
                })
                records.append(record)
                continue

            baseline = await _generate(base_messages)
            cloned = await _generate(clone_messages)
            # 固定按摘要哈希决定 A/B 顺序，结果可复现且候选位置不泄漏真实身份
            swap = int(hashlib.sha256(
                f"{name}|{record['case']}".encode("utf-8")
            ).hexdigest(), 16) % 2 == 1
            candidate_a, candidate_b = (
                (cloned, baseline) if swap else (baseline, cloned)
            )
            judge = await _judge(
                style_hint=style_hint,
                context=f"历史：{case.get('history', [])}\n对方：{user_input}",
                reference=reference,
                a=candidate_a,
                b=candidate_b,
            )
            winner = judge.get("style_winner")
            if winner == "tie":
                net = 0
            elif (winner == "A" and not swap) or (winner == "B" and swap):
                net = 1
            elif winner in {"A", "B"}:
                net = -1
            else:
                net = 0
            net_wins.append(net)
            leaks = _leaked_spans(
                cloned, list(case.get("forbidden_leak_spans", []))
            )
            leak_count += len(leaks)
            record.update({
                "baseline": baseline,
                "clone": cloned,
                "baseline_metrics": _surface_metrics(baseline, reference),
                "clone_metrics": _surface_metrics(cloned, reference),
                "judge": judge,
                "net_win": net,
                "privacy_leaks": leaks,
            })
            records.append(record)
            print(
                f"[{name}] {record['case']}: net={net:+d} "
                f"winner={winner} leak={len(leaks)}"
            )

    summary: dict[str, Any] = {"cases": len(records)}
    if not dry_run:
        base_gaps = [
            r["baseline_metrics"]["char_gap"] for r in records
            if "baseline_metrics" in r
        ]
        clone_gaps = [
            r["clone_metrics"]["char_gap"] for r in records
            if "clone_metrics" in r
        ]
        ci_low, ci_high = _bootstrap_ci(net_wins)
        summary.update({
            "wins": sum(w == 1 for w in net_wins),
            "ties": sum(w == 0 for w in net_wins),
            "losses": sum(w == -1 for w in net_wins),
            "net_win_rate": round(sum(net_wins) / len(net_wins), 4) if net_wins else 0.0,
            "net_win_ci95": [round(ci_low, 4), round(ci_high, 4)],
            "baseline_char_gap_median": median(base_gaps) if base_gaps else None,
            "clone_char_gap_median": median(clone_gaps) if clone_gaps else None,
            "privacy_leaks": leak_count,
        })
    return {"summary": summary, "cases": records}


def main() -> None:
    parser = argparse.ArgumentParser(description="聊天记录克隆专用离线评测")
    parser.add_argument("--cases", required=True)
    parser.add_argument("--out", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-non-test", action="store_true")
    parser.add_argument("--gate", action="store_true")
    args = parser.parse_args()

    cases_doc = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    result = asyncio.run(run(
        cases_doc, dry_run=args.dry_run, allow_non_test=args.allow_non_test,
    ))
    text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    print("\n=== 汇总 ===")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"结果已写入 {args.out}")

    if args.gate and not args.dry_run:
        summary = result["summary"]
        if summary["privacy_leaks"]:
            raise SystemExit("克隆评测门禁失败：出现隐私片段泄漏")
        if summary["net_win_ci95"][0] <= 0:
            raise SystemExit("克隆评测门禁失败：风格净胜 95% CI 未高于 0")


if __name__ == "__main__":
    main()
