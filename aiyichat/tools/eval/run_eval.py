"""对话质量离线评估工具（LLM-as-a-Judge + 确定性 AI 腔门禁 + GSB 成对比较）。

三件事，缺一不可：
1. **评的是生产链路**：走真实的 ``PromptAssembler`` 组装 Prompt，线上注入的时间上下文、
   记忆分层、心情、策略都参与——否则机制改了分数不会动，评测就成了自欺；
   本轮的长度引导与味道允许（``emotion_tag`` 驱动）也从同一入口取，漏一个就测不到对应机制；
2. **确定性门禁**：``ai_tells`` 的 blocking 命中数不需要 LLM，比" judge 打个 3.8 分"
   更能定位问题；口径与生产一致——**先清洗再扫**，
   屏幕上根本不会出现的问题不算缺陷；同理字数与味道一律量清洗后那份；
3. **GSB 成对比较**：同一用例跑改动前/后两份结果，统计 Good/Same/Bad。
   绝对分在不同模型间不可比，成对比较才是"这次改动到底有没有变好"的答案。
   裁判均分实测噪声 ±15%，所以 GSB 同时打印字数与味道命中率两行确定性对照。

用法：
    python tools/eval/run_eval.py                        # 内置用例，调 LLM
    python tools/eval/run_eval.py --dry-run              # 不调 LLM，只校验流水线与用例结构
    python tools/eval/run_eval.py --out before.json      # 落一份结果作为基线
    python tools/eval/run_eval.py --compare before.json  # 与基线做 GSB 成对比较
    python tools/eval/run_eval.py --cases my.json        # 自定义用例
    python tools/eval/run_eval.py --gate                 # blocking 命中即非零退出（评测门禁，需 LLM 凭据，不进 GitHub Actions CI）

依赖运行时模型配置（管理后台「AI 模型配置」，或首次启动由 `.env` 的 DASHSCOPE_API_KEY 生成）；
未配置时请使用 --dry-run 验证流水线。
"""

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
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
    "- persona_consistency：回复是否始终符合角色设定，无出戏/自曝 AI\n"
    "- empathy：是否恰当共情、贴合对方情绪。判据要可核对：对方在报喜（如升职、上岸）"
    "或倾诉（如难过、孤单、撑不住）时，若回复只陈述事实或只给建议、"
    "没有任何先给反应的情绪表达（一起高兴、接住情绪，可核对的"
    "标志是语气词或表情），视为只完成信息传递，empathy 不得高于 2 分\n"
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


def _emotion_tag(user_input: str) -> str:
    """取本轮情绪标签，口径与生产一致（`detect_emotion` 纯关键词那一步）。

    评测刻意只用关键词检测、不补 LLM 降级：裁判要量的是"机制有没有生效"，
    多一次 LLM 推断只会把噪声引进输入侧。生产链路本就以此结果为底线
    （流式侧 `allow_llm_fallback=False` 走的正是这一条）。
    """
    from app.conversation.emotion_detector import detect_emotion

    return detect_emotion(user_input) or ""


def _build_messages(case: dict[str, Any]) -> list[dict[str, str]]:
    """用生产链路的 PromptAssembler 组装本轮消息。

    评测必须评线上真正发出去的那份 Prompt：时间上下文、记忆分层、重要规则
    （不许虚构共同回忆等）都在里面，绕过它自己拼 Prompt 就测不出机制改动。
    长度引导同样要带上——它是本轮"发几条、多长"的唯一来源，漏了它长度改动测不出来。
    `emotion_tag` 也必须给：味道允许（G34）挂在它上面，漏传就只有"我升职了！！！"
    这种靠连跑感叹号兜底的用例能命中，"今天又一个人吃饭"这类倾诉样本全程测不到机制。
    """
    from app.conversation.engine import ConversationEngine
    from app.llm.prompt_assembler import PromptAssembler

    user_input = case["user_input"]
    return PromptAssembler().assemble(
        _build_profile(case),
        _build_history(case),
        user_input,
        memory_facts=list(case.get("memory", [])),
        length_instruction=ConversationEngine._length_instruction_for(
            "standard", user_input, _emotion_tag(user_input)
        ),
    )


def _clean_reply(raw: str) -> str:
    """按生产口径清洗回复文本（去 [IMG:] / [表情:] 等内部标记）。

    长度与 AI 腔两把尺子都必须量"用户真正会看到的那份文本"：
    线上是先清洗再放行，拿模型原始输出去量会把 150 字的内部标记算进正文，
    也会把清洗链本就拦掉的问题当成缺陷报给门禁。
    """
    from app.conversation.response_processor import ResponseProcessor

    cleaned, _images, _stickers = ResponseProcessor().process((raw or "").strip())
    return cleaned.strip()


def _length_metrics(response: str, user_input: str) -> dict[str, Any]:
    """确定性长度指标（G35）。

    裁判分噪声大到能自己造出 -15% 的假退步，而字数与条数是零噪声的尺子：
    改没改短、是不是把一条 300 字长文砸成一屏，看这几个数就够了。
    """
    from app.config import settings
    from app.conversation.message_splitter import split_for_pacing

    raw = (response or "").strip()
    text = _clean_reply(raw)
    bubbles = split_for_pacing(
        text,
        max_messages=settings.multi_message_max_count,
        auto_split_threshold=settings.pacing_auto_split_threshold,
        target_chunk_chars=settings.pacing_target_chunk_chars,
        min_chunk_chars=settings.pacing_min_chunk_chars,
    )
    explicit = [p for p in raw.split("|||") if p.strip()]
    sizes = sorted(len(b.strip()) for b in bubbles if b.strip())
    user_chars = len((user_input or "").strip())
    return {
        "chars": len(text),
        "bubbles": len(bubbles),
        # 模型自己标出来的条数（||| 与空行）：与 bubbles 不同说明是"被切开"而不是"想分条"
        "explicit_bubbles": len(explicit),
        "bubble_median": sizes[len(sizes) // 2] if sizes else 0,
        "bubble_max": sizes[-1] if sizes else 0,
        "ratio": round(len(text) / max(user_chars, 1), 2),
        "list_markers": len(_LIST_LINE_RE.findall(text)),
    }


# 清单体特征行：编号、括号编号、圈号与项目符号（真人微信不这么写）
_LIST_LINE_RE = re.compile(
    r"^\s*(?:\d+[.)、]|[（(]\d+[)）]|[①-⑩]|[-*•·])\s*\S", re.MULTILINE
)

# 味道（G34）确定性判据：emoji 与句末语气词。长度有尺子而味道没有，
# "短而有味"这轮就只能靠裁判主观分判定，而那把尺子实测噪声 ±15%。
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF]"
)
_WARMTH_PARTICLES = "呀啊吧嘛哦哈呢啦呗喽咯哟嘞哇"
# 强弱两档：实测"好吧 我知道了"这类纯应答也带句末语气词，把它算成情绪外显会让
# 尺子虚报命中、把敷衍回成"有味道"。弱档单独计数、不参与 hit 判定。
_STRONG_PARTICLES = "呀哇哈嘞喽咯哟啦哎欸"
# 按句切开后看收尾：真人微信的语气词在句末，句中出现（"好吧我知道了"）不算外显
_SENTENCE_SPLIT_RE = re.compile(r"[!！?？。;；,，~～\s]+")
# 语气词后面常跟一个表情或省略号收尾（"恭喜呀🥳"、"抱抱🤗"），
# 直接把"句末字符"当判据会被 emoji 挡掉，得先剔掉尾部装饰再取尾字
_SENTENCE_TAIL_STRIP = " \t\r\n…‥.。．-—*~～、”\"'』」）)"


def _text_emoji_count(warmth: dict[str, Any]) -> int:
    """正文里模型自己打出的 emoji 数（扣掉动作神态括号被清洗链转出来的那一份）。"""
    return int(warmth.get("emoji", 0)) - len(warmth.get("mapped_emojis", []))


def _warmth_expected(user_input: str) -> bool:
    """这一例"该不该有味道"——判据与生产 `_warmth_instruction` 同源，但**忽略开关本身**。

    绝不拿"引导文本里有没有'倾诉'二字"当判据：`_LENGTH_SOFT_GUIDE` 本就含
    "倾诉"，那样数出来的是满屏假阳性。
    也绝不直接调 `_warmth_instruction`：它第一行就读 `reply_warmth_enabled`，
    关掉开关做对照时"应命中"会跟着变成 0 例，两份结果从此没法比——
    尺子的期待必须钉死在机制的判定条件上，与被测开关无关。
    """
    from app.conversation import engine as E

    tag = _emotion_tag(user_input)
    text = user_input or ""
    if tag in E._WARMTH_CELEBRATE_TAGS or E._CELEBRATION_RE.search(text):
        return True
    return bool(tag in E._WARMTH_VENT_TAGS or E._VENT_RE.search(text))


def _warmth_metrics(response: str, user_input: str) -> dict[str, Any]:
    """确定性味道指标：报喜/倾诉时那一句短话里有没有情绪外显符号。

    emoji 必须分开记来源。清洗链会把 `（微笑）` 转成 `😊`、`(开心)` 转成 `😄`
    （`[IMG:…]` / `[表情:…]` 则直接删除），所以量清洗后那份看似"带表情"的回复，
    可能恰恰是模型写了动作神态标注——那是要扣分的非人信号，绝不能记成味道。
    另外语气词分强弱：`好吧` 这类纯应答带句末语气词却不带情绪，只数强档。
    """
    raw = (response or "").strip()
    text = _clean_reply(raw)
    raw_emojis = {c for c in _EMOJI_RE.findall(raw)}
    clean_emojis = {c for c in _EMOJI_RE.findall(text)}
    mapped_emojis = sorted(clean_emojis - raw_emojis)
    particles: list[str] = []
    for seg in _SENTENCE_SPLIT_RE.split(text):
        tail = _EMOJI_RE.sub("", seg).strip().rstrip(_SENTENCE_TAIL_STRIP)
        if tail and tail[-1] in _WARMTH_PARTICLES:
            particles.append(tail[-1])
    strong = [c for c in particles if c in _STRONG_PARTICLES]
    return {
        "expected": _warmth_expected(user_input),
        "emoji": len(clean_emojis),
        "emoji_chars": sorted(clean_emojis),
        # 由动作神态括号转出的 emoji，不计入"文本里的味道"
        "mapped_emojis": mapped_emojis,
        "particles": len(particles),
        "particle_chars": sorted(set(particles)),
        "strong_particles": sorted(set(strong)),
        "hit": bool(clean_emojis - set(mapped_emojis)) or bool(strong),
    }


def _scan_ai_tells(response: str) -> dict[str, Any]:
    """确定性 AI 腔门禁：以清洗后的正文为准（生产口径），原始输出只作对照。

    `raw_blocking_count` 与 `blocking_count` 的差值说明清洗链自己拦掉了多少，
    两者混用会把"屏幕上根本不会出现的问题"记成缺陷。
    """
    from app.conversation import ai_tells

    raw_result = ai_tells.scan(
        (response or "").strip(), keep_if_all_blocked=False
    )
    cleaned_result = ai_tells.scan(_clean_reply(response), keep_if_all_blocked=False)
    return {
        "blocking": cleaned_result.blocking,
        "blocking_count": len(cleaned_result.blocking),
        "review": cleaned_result.review,
        "vocab": cleaned_result.vocab,
        "raw_blocking": raw_result.blocking,
        "raw_blocking_count": len(raw_result.blocking),
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


async def _bootstrap() -> None:
    """加载运行时配置：模型与 config_id 都存在 DB 的 app_settings 文档里。

    本脚本是一次性进程，不走 FastAPI 的 lifespan，不调 `settings.reload()`
    就只会读到 `.env`/代码默认值，`text-default` 这类 config_id 解析不到，
    每个用例都会以"未引用有效的基础配置"失败。
    """
    from app.config import settings
    from app.db import mongo

    await mongo.connect_db()
    await settings.reload()


async def run(cases: list[dict], *, dry_run: bool) -> list[dict[str, Any]]:
    """执行评估，打印汇总并返回逐用例结果（供 --out/--compare 复用）。"""
    print(f"=== 对话质量评估：{len(cases)} 个用例，dry_run={dry_run} ===\n")
    try:
        await _bootstrap()
    except Exception as e:  # 本地无 DB 时仍可跑 dry-run 校验流水线
        print("!! 运行时配置加载失败，将使用代码默认值:", e)
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
            print(
                "  (dry-run) 情绪标签:",
                _emotion_tag(case["user_input"]) or "(无)",
                f"味道应命中={_warmth_expected(case['user_input'])}",
            )
            print("  (dry-run) 记忆:", case.get("memory", []))
            results.append(record)
            print()
            continue

        try:
            response = await _generate_response(case)
            scores = await _judge(case, response)
            tells = _scan_ai_tells(response)
            lengths = _length_metrics(response, case["user_input"])
            warmth = _warmth_metrics(response, case["user_input"])
            print("  回复:", response[:80])
            print("  评分:", json.dumps(scores, ensure_ascii=False))
            raw_extra = tells["raw_blocking_count"] - tells["blocking_count"]
            print(
                "  AI腔:",
                f"blocking={tells['blocking_count']} {tells['blocking']}",
                f"(清洗前 {tells['raw_blocking_count']}，清洗链拦掉 {raw_extra})",
                f"review={tells['review']} vocab={tells['vocab']}",
            )
            print(
                "  长度:",
                f"{lengths['chars']}字/{lengths['bubbles']}条",
                f"单条中位{lengths['bubble_median']}",
                f"镜像比{lengths['ratio']}x",
                f"清单行{lengths['list_markers']}",
            )
            print(
                "  味道:",
                f"应命中={warmth['expected']} 命中={warmth['hit']}",
                f"emoji{warmth['emoji_chars']}",
                f"语气词{warmth['particle_chars']}(强{warmth['strong_particles']})",
                f"括号转出的表情{warmth['mapped_emojis']}",
            )
            record.update(
                {
                    "response": response,
                    "scores": scores,
                    "ai_tells": tells,
                    "length": lengths,
                    "warmth": warmth,
                }
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
        _print_length_summary(results)
        _print_warmth_summary(results)
    elif dry_run:
        print("dry-run 流水线校验通过（含生产 Prompt 组装）。")
    return results


def _percentile(values: list[int], pct: float) -> int:
    """小样本百分位（取最近秩），长度指标用。"""
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * pct))]


def _print_length_summary(results: list[dict[str, Any]]) -> None:
    """长度汇总：整体分布 + 按用例前缀分桶（闲聊与倾诉不该用同一把尺子量）。"""
    items = [r for r in results if r.get("length")]
    if not items:
        return
    chars = [r["length"]["chars"] for r in items]
    bubbles = [r["length"]["bubbles"] for r in items]
    explicit = [r["length"]["explicit_bubbles"] for r in items]
    markers = sum(r["length"]["list_markers"] for r in items)
    print("=== 长度指标（确定性，零噪声）===")
    print(
        f"  整轮字数: 中位 {_percentile(chars, 0.5)} / p90 {_percentile(chars, 0.9)}"
        f" / 最长 {max(chars)}"
    )
    print(
        f"  气泡条数: 中位 {_percentile(bubbles, 0.5)} / 最多 {max(bubbles)}"
        f"（模型自标 ||| 条数中位 {_percentile(explicit, 0.5)}）"
    )
    ratio_median = round(median(r["length"]["ratio"] for r in items), 2)
    print(f"  回复/对方长度比: 中位 {ratio_median}x")
    print(f"  清单体行数: {markers}")
    buckets: dict[str, list[int]] = {}
    for r in items:
        key = r["name"].split("-", 1)[0]
        buckets.setdefault(key, []).append(r["length"]["chars"])
    for key, values in sorted(buckets.items()):
        print(f"    - {key}: 中位 {_percentile(values, 0.5)} 字（{len(values)} 例）")


def _print_warmth_summary(results: list[dict[str, Any]]) -> None:
    """味道汇总（确定性）：应命中几例、实际命中几例，逐例点名漏掉的。

    长度收住之后塌掉的就是这一项——同批 13 用例的 emoji 命中曾从 11/13 掉到 6/13。
    只看整体命中率看不出问题，必须看"该有味道的那几例"有没有被回成干信息。
    """
    items = [r for r in results if r.get("warmth")]
    if not items:
        return
    expected = [r for r in items if r["warmth"]["expected"]]
    hit_all = [r for r in items if r["warmth"]["hit"]]
    hit_expected = [r for r in expected if r["warmth"]["hit"]]
    # 只数正文里模型自己打出的 emoji：动作神态括号转出来的另算一行
    text_emoji = [r for r in items if _text_emoji_count(r["warmth"])]
    mapped = [r for r in items if r["warmth"]["mapped_emojis"]]
    print("=== 味道指标（确定性，零噪声）===")
    print(
        f"  应命中 {len(expected)}/{len(items)} 例，"
        f"其中实际带情绪外显: {len(hit_expected)}/{len(expected)}"
    )
    print(
        f"  全批正文 emoji 命中: {len(text_emoji)}/{len(items)}"
        f"（历史基线 6/13，味道机制上线前 11/13）"
    )
    print(f"  全批带情绪外显（强语气词或正文 emoji）: {len(hit_all)}/{len(items)}")
    if mapped:
        # 这批不是味道而是缺陷：括号里写神态，清洗链才把它变成 😊
        print(
            f"  ⚠ 靠动作神态括号转出 emoji 的用例: {len(mapped)}/{len(items)}"
            f" —— {', '.join(r['name'] for r in mapped)}"
        )
    for r in expected:
        if not r["warmth"]["hit"]:
            print(f"    - 缺失: {r['name']}")


def _gsb(current: list[dict], baseline: list[dict]) -> None:
    """GSB 成对比较：同一用例取两份 overall 分，统计 Good/Same/Bad。"""
    base_map = {r["name"]: r for r in baseline}
    good = same = bad = 0
    len_pairs: list[tuple[int, int]] = []
    warmth_pairs: list[tuple[int, int]] = []
    emoji_hits: list[tuple[int, int]] = []
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
        # 旧基线 JSON 没有 length 字段（指标是本轮才加的），退回按原始字符数对照
        cur_len = item.get("length", {}).get("chars") or len(
            (item.get("response") or "").strip()
        )
        base_len = base.get("length", {}).get("chars") or len(
            (base.get("response") or "").strip()
        )
        len_pairs.append((base_len, cur_len))
        cur_w = item.get("warmth", {})
        base_w = base.get("warmth", {})
        warmth_note = ""
        if cur_w and base_w and cur_w.get("expected"):
            # 基线那份必须也有味道字段，否则把"没测过"当成"没命中"来对照
            cur_hit = 1 if cur_w.get("hit") else 0
            base_hit = 1 if base_w.get("hit") else 0
            warmth_pairs.append((base_hit, cur_hit))
            warmth_note = f", 味道 {base_hit} → {cur_hit}"
        if cur_w and base_w:
            emoji_hits.append(
                (
                    1 if _text_emoji_count(base_w) else 0,
                    1 if _text_emoji_count(cur_w) else 0,
                )
            )
        print(
            f"  {item['name']}: {verdict} "
            f"({base_score:.2f} → {cur_score:.2f}, "
            f"blocking {base_block} → {cur_block}, "
            f"字数 {base_len} → {cur_len}{warmth_note})"
        )
    print(f"  合计 Good={good} Same={same} Bad={bad}")
    if good + bad:
        print(f"  净改善率: {(good - bad) / (good + same + bad):.0%}")
    if len_pairs:
        # 字数是中位数可直接对照的确定性量：裁判分噪声 ±15%，这个不是
        base_median = median(p[0] for p in len_pairs)
        cur_median = median(p[1] for p in len_pairs)
        print(
            f"  整轮字数中位: {base_median:.0f} → {cur_median:.0f}"
            f"（{(cur_median - base_median) / max(base_median, 1):+.0%}）"
        )
    if warmth_pairs:
        # 味道命中率同样是计数而非主观分：这一行才是 G34 那轮的判据
        print(
            f"  应命中用例的味道命中率: "
            f"{sum(p[0] for p in warmth_pairs)}/{len(warmth_pairs)} → "
            f"{sum(p[1] for p in warmth_pairs)}/{len(warmth_pairs)}"
        )
    if emoji_hits:
        print(
            f"  全批正文 emoji 命中: "
            f"{sum(p[0] for p in emoji_hits)}/{len(emoji_hits)} → "
            f"{sum(p[1] for p in emoji_hits)}/{len(emoji_hits)}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="对话质量离线评估（LLM-as-a-Judge）")
    parser.add_argument("--cases", help="自定义用例 JSON 文件路径")
    parser.add_argument("--dry-run", action="store_true", help="不调用 LLM，仅校验流水线")
    parser.add_argument("--out", help="把逐用例结果写入 JSON，作为后续 GSB 的基线")
    parser.add_argument("--compare", help="与已有结果 JSON 做 GSB 成对比较")
    parser.add_argument(
        "--gate",
        action="store_true",
        help="任一用例在清洗后仍有 blocking 级 AI 腔即以非零码退出（评测门禁，需模型凭据，不属 CI 流水线）",
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
