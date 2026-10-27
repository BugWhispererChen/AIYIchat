"""Admin 功能实验室路由。

集中管理对话引擎中"可灰度"的能力开关（默认关闭或需依赖外部密钥的能力），
以可视化 Toggle 的形式展示，保存后热重载生效。

与 multimedia 配置共享同一套持久化机制（AppSettingsRepository.upsert + settings.reload），
但仅聚焦布尔类能力开关，降低运营心智负担。
"""

from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.admin.auth import require_admin
from app.admin.routes.common import _render

features_router = APIRouter(tags=["admin"])


@dataclass(frozen=True)
class FeatureToggle:
    """单个能力开关的元信息（用于渲染与保存）。"""

    key: str          # settings / AppSettingsDocument 中的字段名
    label: str        # 展示名称
    desc: str         # 能力说明
    dependency: str   # 依赖提示（无依赖则空）
    cost: str         # 成本标签：free / llm / dashscope


# 可在后台切换的能力开关清单（顺序即展示顺序）
FEATURE_TOGGLES: list[FeatureToggle] = [
    FeatureToggle(
        key="support_strategy_enabled",
        label="情感支持策略",
        desc="基于 Hill 助人三阶段（探索→安慰→行动）动态选择支持策略，提升共情深度。",
        dependency="",
        cost="free",
    ),
    FeatureToggle(
        key="consistency_check_enabled",
        label="人设一致性自检",
        desc="拦截 AI 自曝身份（如'我是AI'）与禁用词泄漏，防止人设穿帮，兼顾合规。",
        dependency="",
        cost="free",
    ),
    FeatureToggle(
        key="message_time_enabled",
        label="对话时间感知",
        desc="注入当前时间并按会话间隔为历史消息打时间标签，防止角色把昨天/前天的消息当作刚刚发生。",
        dependency="",
        cost="free",
    ),
    FeatureToggle(
        key="time_anchor_enabled",
        label="时间锚点（日期基准）",
        desc="在系统提示词开头声明“今天是几号”，作为全部相对时间（今天/昨天/最近）的推算基准，"
              "避免角色读到旧记忆时还不知道当天日期而算错天数。",
        dependency="需先开启「对话时间感知」总开关",
        cost="free",
    ),
    FeatureToggle(
        key="time_idle_hint_enabled",
        label="对话间隔语义提示",
        desc="告知角色“距对方上一条消息已过去多久”（如 3 天 2 小时），无需模型自己做日期减法；"
              "仅统计对方真实发送的消息，主动关怀不重置空闲计时。",
        dependency="需先开启「对话时间感知」总开关",
        cost="free",
    ),
    FeatureToggle(
        key="time_day_separator_enabled",
        label="跨天分隔符",
        desc="历史消息自然日发生变化时插入一行“以下对话发生在 X 月 X 日，距现在 N 天”，"
              "修正“跨天但间隔不足阈值”时漏标的盲区，比逐条标签更省 token。",
        dependency="需先开启「对话时间感知」总开关",
        cost="free",
    ),
    FeatureToggle(
        key="memory_inject_time_suffix",
        label="记忆时间后缀",
        desc="为注入的每条长期记忆追加“（记录于 N 天前）”或“（长期信息）”，"
              "防止角色把几个月前的一次性事件（如已结束的面试）当作即将发生。",
        dependency="需先开启「对话时间感知」总开关",
        cost="free",
    ),
    FeatureToggle(
        key="time_jionlp_enabled",
        label="相对时间归一化（jionlp）",
        desc="用 jionlp 把“下周三”“9月15号”等相对时间精确归一化为绝对日期，"
              "并校验 LLM 抽取的日期是否真能从原文推出（防幻觉）。"
              "依赖缺失或解析失败时自动降级为纯 LLM，不影响主链路。",
        dependency="需安装 jionlp（约 150MB，含词典与 numpy）",
        cost="free",
    ),
    FeatureToggle(
        key="message_time_tag_year_enabled",
        label="跨年时间标签带年份",
        desc="历史消息与当前不同年时标签输出 [2025-12-31 23:45]（同年仍为 [08-13 23:45]），"
              "避免跳年会话中模型无法区分去年与今年。",
        dependency="需先开启「对话时间感知」总开关",
        cost="free",
    ),
    FeatureToggle(
        key="emotion_llm_fallback_enabled",
        label="LLM 隐含情绪识别",
        desc="关键词未命中时调用轻量 LLM 识别隐含情绪（如'又加班到十点'→疲惫）。",
        dependency="每轮消息可能额外增加 1 次 LLM 调用",
        cost="llm",
    ),
    FeatureToggle(
        key="character_mood_enabled",
        label="角色心情系统",
        desc="角色拥有自己的情绪状态并随对话演化（开心/失落/想念等），联动语气、语速与图片表情，增强真人感。",
        dependency="每轮对话后额外增加 1 次轻量 LLM 调用（异步，不阻塞回复）",
        cost="llm",
    ),
    FeatureToggle(
        key="proactive_event_enabled",
        label="事件驱动主动关怀",
        desc=(
            "控制到期后是否**主动发消息**跟进对方提过的事（面试/考试等）。"
            "事件识别与对话中的自然关心始终启用，与本开关无关。"
        ),
        dependency="依赖焦点抽取（长期记忆）正常工作",
        cost="llm",
    ),
    FeatureToggle(
        key="proactive_yield_enabled",
        label="主动关怀退让（被忽略就收着）",
        desc="最近连续若干条主动消息都没得到对方回应时，主动放弃当前阶段的召回、"
        "并在记录里留一条 skipped 痕迹（后台能看到「想发但忍住」）；对方一旦开口即自动恢复。"
        "陪伴的诚意在于不打扰，不在于敲门次数。",
        dependency="阈值与每日上限在「主动关怀」页配置；关闭即回退为只看「多久没活跃」",
        cost="free",
    ),
    FeatureToggle(
        key="profile_routine_enabled",
        label="角色作息（此刻你在做什么）",
        desc="按全局时区与角色作息时段表，在动态尾段注入一行「现在是周三 14:20，你刚下课，"
        "正在食堂排队」。**只改变说什么怎么说，不改变消息是否送达、也不延迟回复**；"
        "未配时段表的角色不受影响。",
        dependency="需在角色详情「作息时段」里为该角色配置时段表；作息属角色，不按对方时区计算",
        cost="free",
    ),
    FeatureToggle(
        key="clone_retrieval_enabled",
        label="克隆语料动态检索",
        desc="从聊天记录导入时保留的高质量情境片段中，按当前情境检索两组「对方这样说时，"
        "角色曾怎样回复」作为风格参考。参考片段只放动态尾段，并要求模型模仿表达方式、"
        "不得复述其中事实；关闭或未授权保留语料的角色完全不受影响。",
        dependency="导入时需勾选「保留脱敏情境片段」；无向量时自动降级为字符与场景检索",
        cost="free",
    ),
    FeatureToggle(
        key="embedding_enabled",
        label="语义记忆检索",
        desc=(
            "将长期记忆向量化，按语义相似度检索注入，显著提升'记得住'的体验。"
            "注入已按稳定性分层（常驻事实留前缀、本轮召回结果后置），不会击穿 prompt 前缀缓存。"
        ),
        dependency="需在「AI 模型配置」中为 embedding 功能配置 OpenAI 兼容的向量化服务（如百炼、OpenAI、硅基流动等）",
        cost="dashscope",
    ),
    FeatureToggle(
        key="embedding_breaker_enabled",
        label="向量化失败熔断",
        desc="连续失败（免费额度耗尽、端点故障）达窗口阈值后暂停向量化 10 分钟并定期试探恢复，"
        "期间记忆检索自动走规则排序，避免每条消息都白打一次 HTTP 且失败只落在日志里。",
        dependency="仅在「语义记忆检索」开启时起作用；关闭则回到失败也照打的行为",
        cost="free",
    ),
    FeatureToggle(
        key="function_calling_enabled",
        label="自主工具调用",
        desc="允许模型自主决定检索记忆或生成配图（Function Calling）。",
        dependency="需所选对话模型支持 tools 调用",
        cost="llm",
    ),
    FeatureToggle(
        key="sticker_enabled",
        label="表情包发送",
        desc="AI 根据对话语境自主发送表情包（[表情:描述] 标签 + 语义检索），"
        "理解对方发表的表情包含义，并支持以图回图斗图。",
        dependency="需表情库非空（随包预置资源自动入库，可在「表情包管理」页上传补充）；"
        "开启语义检索时匹配更精准",
        cost="free",
    ),
    FeatureToggle(
        key="memory_reflection_enabled",
        label="反思层（高阶判断）",
        desc="在记忆事实积累到一定规模后，额外用一次轻量模型调用把多条事实归纳成"
        "「对方这三周一直在加班」这类跨时间的判断，常驻注入角色提示词。"
        "每条判断必须能点出它依据的原始记忆，依据不足的会被直接丢弃；"
        "判断可在「记忆管理」页逐条撤销。",
        dependency="依赖记忆抽取已产出足量事实（默认门槛 12 条）；"
        "每次触发多 1 次模型调用，按最小间隔 24 小时摊销",
        cost="llm",
    ),
    FeatureToggle(
        key="memory_hygiene_enabled",
        label="记忆治理（指称归一与点状时效）",
        desc="抽取落库时把\"用户…\"\"…与AI的关系\"这类第三人称/出戏写法改成\"对方…\"\"与我…\"，"
        "并标记那些\"未来某一时刻的状态\"（如 8月26日有面试）；事件时刻过了宽限期后不再注入，"
        "避免角色几个月后还把已结束的面试当作近况关心。存量脏数据需在服务器执行一次治理迁移。",
        dependency="点状时效依赖「相对时间归一化（jionlp）」产出可靠的 valid_at；"
        "关闭时只影响新写入的打标，已打标的存量事实仍会按新规则剔除",
        cost="free",
    ),
    FeatureToggle(
        key="memory_graph_enabled",
        label="人际关系网络图查询增强（Kuzu）",
        desc="启用后实体/关系同步写入嵌入式图库，多跳关系查询走 Cypher（如'小红的哥哥的同事'）；"
        "未启用自动降级为 MongoDB 一跳查询，功能完整可用。",
        dependency="kuzu 随包安装，图数据文件挂卷持久化（data/entity_graph）；"
        "开启后首次使用自动建库，无需额外配置",
        cost="free",
    ),
    FeatureToggle(
        key="tts_expression_enabled",
        label="语音情感表现力",
        desc="根据对方情绪与角色心情动态生成语音指令/情感标签/音调修饰，让语音回复更拟人化；"
        "关闭后语音无任何表现力修饰。",
        dependency="需先在「AI 模型配置」开启 TTS 语音合成",
        cost="free",
    ),
    FeatureToggle(
        key="voice_dynamic_enabled",
        label="语音动态触发窗口",
        desc="对方发语音触发语音回复后，该会话在随机时长的固定窗口内，"
        "文字消息也以语音回复；窗口到期自动回落为仅语音触发。"
        "窗口时长在下方「语音动态触发参数」中配置。",
        dependency="需先在「AI 模型配置」开启 TTS 语音合成",
        cost="free",
    ),
    FeatureToggle(
        key="web_auth_enabled",
        label="C 端网页登录",
        desc="开启后网页账号可用「用户名 + 密码」登录，并以会话令牌访问流式对话接口；"
        "关闭时 /api/auth/* 与 /web/* 一律 403，QQ / 微信链路完全不受影响。",
        dependency="可选：在「网页账号」页指定默认载体渠道（未指定时账号按需自建）",
        cost="free",
    ),
    FeatureToggle(
        key="web_register_enabled",
        label="C 端自助注册",
        desc="开放网页自助注册：注册时原子建齐账号、额度、身份与对话服务，任一步失败自动回滚，"
        "不会留下占死用户名的半成品账号。",
        dependency="需先开启「C 端网页登录」",
        cost="free",
    ),
    FeatureToggle(
        key="web_register_require_review",
        label="注册需人工审核",
        desc="新账号注册后处于待审核状态，管理员在「网页账号」页通过后方可登录；"
        "被拒绝的用户名 24 小时内不可重新注册。",
        dependency="需先开启「C 端自助注册」",
        cost="free",
    ),
    FeatureToggle(
        key="web_captcha_enabled",
        label="注册/登录图形验证码",
        desc="注册与登录前需完成算式验证码，答案一次性消费（答错即失效），"
        "与 IP 滑动窗口限流共同构成防刷链。",
        dependency="单实例内存实现，多实例部署需改为共享存储",
        cost="free",
    ),
    # —— 流式主链路拟人化（C 端网页）——
    FeatureToggle(
        key="stream_gate_enabled",
        label="流式按句缓冲放行",
        desc="网页流式回复改为「先清洗再放行」：攒到句末边界且确认没有半截标记才下发，"
        "（微笑）/[表情:]/[IMG:]/自称 AI/禁用词不再当场露出，屏幕所见即落库所持。"
        "代价是首字延迟增加约 0.3~1 秒。",
        dependency="关闭时输出与改动前逐字节一致，可随时回退",
        cost="free",
    ),
    FeatureToggle(
        key="stream_pacing_enabled",
        label="气泡真人节奏",
        desc="逐条气泡之间由服务端插入停顿（含首条的思考延迟、深夜减速与偶发长停顿），"
        "不再一次性砸出全部气泡。",
        dependency="需先开启「流式按句缓冲放行」与「拟人节奏参数」",
        cost="free",
    ),
    FeatureToggle(
        key="human_rhythm_enabled",
        label="拟人节奏参数",
        desc="用下方节奏参数取代旧的按字延迟公式；关闭时沿用 legacy 间隔，行为与改动前一致。",
        dependency="影响网页与 QQ/微信渠道的逐条间隔",
        cost="free",
    ),
    FeatureToggle(
        key="stream_jitter_guard_enabled",
        label="流式复读拦截",
        desc="边放行边比「公共前缀占旧回复比例」，判定复读即收回本轮气泡并重跑一次，"
        "避免网页端出现「一条消息发三遍」。",
        dependency="需先开启「流式按句缓冲放行」；重跑会多花一次模型调用",
        cost="paid",
    ),
    # —— 回复长度治理（G35：实测角色整轮中位 99 字 / 4.5 条，对方只说 13 字）——
    FeatureToggle(
        key="reply_length_mirror_enabled",
        label="回复长度镜像",
        desc="按对方这条消息的长短分档提示角色对齐（只给档位词、不给数字）："
        "对方甩几个字就短回一句，对方说了不少才多回一两句。真人话轮本就会互相对齐长度。",
        dependency="与「回复方式」软引导同处动态尾段注入，不进 system 前缀，不影响前缀缓存",
        cost="free",
    ),
    FeatureToggle(
        key="reply_warmth_enabled",
        label="短而有味（报喜/倾诉带反应）",
        desc="对方报喜或倾诉时，允许那一句短话先给情绪反应、带一个语气词或表情"
        "（如「哇 恭喜呀🥳」「心疼你 慢慢说」）。只改这一句承担什么，不放宽长短与条数。",
        dependency="与「回复长度镜像」同时生效；中性消息不注入，避免每条都挂表情腻成模板",
        cost="free",
    ),
    FeatureToggle(
        key="pacing_split_enabled",
        label="超长回复按句末分条",
        desc="单条超过阈值（默认 120 字）时按句末边界切成适合逐条发送的小块，"
        "避免一整段 300 字砸在屏幕上；代码块、表格与编号清单内部不切。",
        dependency="只改「怎么切」不改「说什么」；总字数要靠长度引导与镜像收口",
        cost="free",
    ),
    FeatureToggle(
        key="empty_reply_fallback_enabled",
        label="空回复兜底一句",
        desc="整轮被清洗成空（模型只出了动作/图片/表情标签）且本轮没有任何图片或表情包可发时，"
        "补一句「刚才那句我没说好，你再说一次？」，避免把空串发往平台。",
        dependency="关闭时空回复将直接不发送（QQ 侧空 content 会被平台以 400 拒绝，微信同理）",
        cost="free",
    ),
]


def _pacing_context(s: "Any") -> dict[str, object]:
    """把流式放行/节奏/复读护栏的数值参数摊平成模板上下文。

    GET 与 POST 两条路共用同一个构造函数，避免两处键名漂移
    （本项目已经吃过"配置键登记漏一处、后台改了不生效"的亏）。
    """
    return {
        "stream_gate_max_pending": s.stream_gate_max_pending,
        "stream_max_total_delay_seconds": s.stream_max_total_delay_seconds,
        "human_typing_ms_per_char": s.human_typing_ms_per_char,
        "human_gap_min_ms": s.human_gap_min_ms,
        "human_gap_max_ms": s.human_gap_max_ms,
        "human_startup_min_ms": s.human_startup_min_ms,
        "human_startup_max_ms": s.human_startup_max_ms,
        "human_jitter_ratio": s.human_jitter_ratio,
        "human_night_factor": s.human_night_factor,
        "human_long_pause_prob": s.human_long_pause_prob,
        "stream_jitter_precheck_chars": s.stream_jitter_precheck_chars,
        "stream_jitter_prefix_ratio": s.stream_jitter_prefix_ratio,
        "stream_jitter_overlap_ratio": s.stream_jitter_overlap_ratio,
        "stream_jitter_reset_max": s.stream_jitter_reset_max,
        # 长度治理：条数上限、流式气泡上限、句末分条三阈值
        # （此前 multi_message_max_count 只有代码默认值，后台看着能调其实调不动）
        "multi_message_max_count": s.multi_message_max_count,
        "stream_max_bubbles": s.stream_max_bubbles,
        "pacing_auto_split_threshold": s.pacing_auto_split_threshold,
        "pacing_target_chunk_chars": s.pacing_target_chunk_chars,
        "pacing_min_chunk_chars": s.pacing_min_chunk_chars,
    }


def _pacing_updates(form: Any, s: Any) -> dict[str, object]:
    """读取并限幅节奏参数：表单值非法时保留当前值，区间倒置时自动交换。"""

    def _int_or(name: str, current: int) -> int:
        try:
            return int(str(form.get(name, current)))
        except (TypeError, ValueError):
            return current

    def _float_or(name: str, current: float) -> float:
        try:
            return float(str(form.get(name, current)))
        except (TypeError, ValueError):
            return current

    def _clamp_int(name: str, current: int, lo: int, hi: int) -> int:
        return max(lo, min(hi, _int_or(name, current)))

    def _clamp_pair(lo_name: str, hi_name: str, cur_lo: int, cur_hi: int, lo: int, hi: int) -> tuple[int, int]:
        a = _clamp_int(lo_name, cur_lo, lo, hi)
        b = _clamp_int(hi_name, cur_hi, lo, hi)
        return (a, b) if a <= b else (b, a)

    gap_lo, gap_hi = _clamp_pair(
        "human_gap_min_ms", "human_gap_max_ms",
        s.human_gap_min_ms, s.human_gap_max_ms, 0, 30000,
    )
    # 分条三阈值必须 起切阈值 ≥ 目标长度 ≥ 碎片下限：运营填反了就整体重排，
    # 而不是让 split_for_pacing 拿到 target>threshold 这种切不动的组合
    threshold, target, chunk_floor = sorted(
        (
            _clamp_int(
                "pacing_auto_split_threshold", s.pacing_auto_split_threshold, 20, 600
            ),
            _clamp_int(
                "pacing_target_chunk_chars", s.pacing_target_chunk_chars, 15, 300
            ),
            _clamp_int("pacing_min_chunk_chars", s.pacing_min_chunk_chars, 2, 80),
        ),
        reverse=True,
    )
    boot_lo, boot_hi = _clamp_pair(
        "human_startup_min_ms", "human_startup_max_ms",
        s.human_startup_min_ms, s.human_startup_max_ms, 0, 15000,
    )
    return {
        "stream_gate_max_pending": _clamp_int(
            "stream_gate_max_pending", s.stream_gate_max_pending, 8, 400
        ),
        "stream_max_total_delay_seconds": max(
            0.0,
            min(
                120.0,
                _float_or(
                    "stream_max_total_delay_seconds",
                    s.stream_max_total_delay_seconds,
                ),
            ),
        ),
        "human_typing_ms_per_char": _clamp_int(
            "human_typing_ms_per_char", s.human_typing_ms_per_char, 20, 400
        ),
        "human_gap_min_ms": gap_lo,
        "human_gap_max_ms": gap_hi,
        "human_startup_min_ms": boot_lo,
        "human_startup_max_ms": boot_hi,
        "human_jitter_ratio": max(
            0.0, min(0.9, _float_or("human_jitter_ratio", s.human_jitter_ratio))
        ),
        "human_night_factor": max(
            1.0, min(4.0, _float_or("human_night_factor", s.human_night_factor))
        ),
        "human_long_pause_prob": max(
            0.0,
            min(1.0, _float_or("human_long_pause_prob", s.human_long_pause_prob)),
        ),
        "stream_jitter_precheck_chars": _clamp_int(
            "stream_jitter_precheck_chars", s.stream_jitter_precheck_chars, 8, 400
        ),
        "stream_jitter_prefix_ratio": max(
            0.1,
            min(
                1.0,
                _float_or("stream_jitter_prefix_ratio", s.stream_jitter_prefix_ratio),
            ),
        ),
        "stream_jitter_overlap_ratio": max(
            0.2,
            min(
                1.0,
                _float_or(
                    "stream_jitter_overlap_ratio", s.stream_jitter_overlap_ratio
                ),
            ),
        ),
        "stream_jitter_reset_max": _clamp_int(
            "stream_jitter_reset_max", s.stream_jitter_reset_max, 0, 3
        ),
        "multi_message_max_count": _clamp_int(
            "multi_message_max_count", s.multi_message_max_count, 1, 10
        ),
        "stream_max_bubbles": _clamp_int(
            "stream_max_bubbles", s.stream_max_bubbles, 0, 12
        ),
        "pacing_auto_split_threshold": threshold,
        "pacing_target_chunk_chars": target,
        "pacing_min_chunk_chars": chunk_floor,
    }


@features_router.get("/features", response_class=HTMLResponse)
async def features_page(
    request: Request, _s: str = Depends(require_admin)
) -> HTMLResponse:
    """能力开关配置页面（GET）。"""
    from app.config import settings

    toggles = [
        {
            "key": f.key,
            "label": f.label,
            "desc": f.desc,
            "dependency": f.dependency,
            "cost": f.cost,
            "enabled": bool(getattr(settings, f.key, False)),
        }
        for f in FEATURE_TOGGLES
    ]
    return _render(request, "features.html", {
        "toggles": toggles,
        # 对话时间感知参数区（数值类配置，与开关同页管理）
        "gap_minutes": settings.message_time_gap_minutes,
        "time_session_gap_hours": settings.time_session_gap_hours,
        "time_reunion_gap_hours": settings.time_reunion_gap_hours,
        "tz_offset": settings.default_timezone_offset,
        # 表情包触发参数
        "sticker_casual_probability": settings.sticker_casual_probability,
        "sticker_reply_probability": settings.sticker_reply_probability,
        # 语音动态触发窗口参数
        "voice_window_min_minutes": settings.voice_window_min_minutes,
        "voice_window_max_minutes": settings.voice_window_max_minutes,
        **_pacing_context(settings),
        "active_page": "features",
    })


@features_router.post("/features", response_class=HTMLResponse)
async def features_save(
    request: Request, _s: str = Depends(require_admin)
) -> HTMLResponse:
    """保存能力开关与对话时间感知参数到数据库并热重载。"""
    from app.config import settings
    from app.db.repositories.app_settings_repo import AppSettingsRepository
    from app.llm.provider_factory import reset_provider
    from app.services.embedding_service import reset_embedding_service
    from app.services.entity_graph_service import reset_entity_graph_service
    from app.services.vision_service import reset_vision_service

    form = await request.form()
    # checkbox 未勾选时不出现在表单中，缺失即视为 False
    updates: dict[str, object] = {
        f.key: (form.get(f.key) == "on") for f in FEATURE_TOGGLES
    }

    # 对话时间感知数值参数：标签间隔阈值 + 单一时区源
    # 表单值非法（空/非数字）时保留当前值，避免运营误操作把配置写成 0
    def _int_or(name: str, current: int) -> int:
        try:
            return int(str(form.get(name, current)))
        except (TypeError, ValueError):
            return current

    tz = max(-12, min(14, _int_or("tz_offset", settings.default_timezone_offset)))
    updates["message_time_gap_minutes"] = max(
        0, min(1440, _int_or("gap_minutes", settings.message_time_gap_minutes))
    )
    # 会话间隔阈值限幅，强档不得低于本次保存的软档
    session_gap = max(
        1, min(72, _int_or("time_session_gap_hours", settings.time_session_gap_hours))
    )
    updates["time_session_gap_hours"] = session_gap
    updates["time_reunion_gap_hours"] = max(
        session_gap, min(720, _int_or("time_reunion_gap_hours", settings.time_reunion_gap_hours))
    )
    updates["default_timezone_offset"] = tz

    # 表情包触发概率参数（浮点数，0~1 范围）
    def _float_or(name: str, current: float) -> float:
        try:
            return float(str(form.get(name, current)))
        except (TypeError, ValueError):
            return current

    updates["sticker_casual_probability"] = max(
        0.0, min(1.0, _float_or("sticker_casual_probability", settings.sticker_casual_probability))
    )
    updates["sticker_reply_probability"] = max(
        0.0, min(1.0, _float_or("sticker_reply_probability", settings.sticker_reply_probability))
    )

    # 语音动态触发窗口时长（分钟，1~1440）；min > max 时自动交换，避免窗口逻辑异常
    voice_min = max(
        1, min(1440, _int_or("voice_window_min_minutes", settings.voice_window_min_minutes))
    )
    voice_max = max(
        1, min(1440, _int_or("voice_window_max_minutes", settings.voice_window_max_minutes))
    )
    if voice_min > voice_max:
        voice_min, voice_max = voice_max, voice_min
    updates["voice_window_min_minutes"] = voice_min
    updates["voice_window_max_minutes"] = voice_max
    # 流式放行与节奏参数
    updates.update(_pacing_updates(form, settings))

    repo = AppSettingsRepository()
    await repo.upsert(updates)

    # 热重载并重置相关缓存（embedding 开关变化需重建服务；图服务开关需重置单例）
    await settings.reload()
    reset_provider()
    reset_vision_service()
    reset_embedding_service()
    reset_entity_graph_service()

    toggles = [
        {
            "key": f.key,
            "label": f.label,
            "desc": f.desc,
            "dependency": f.dependency,
            "cost": f.cost,
            "enabled": bool(getattr(settings, f.key, False)),
        }
        for f in FEATURE_TOGGLES
    ]
    return _render(request, "features.html", {
        "toggles": toggles,
        "gap_minutes": settings.message_time_gap_minutes,
        "time_session_gap_hours": settings.time_session_gap_hours,
        "time_reunion_gap_hours": settings.time_reunion_gap_hours,
        "tz_offset": settings.default_timezone_offset,
        "sticker_casual_probability": settings.sticker_casual_probability,
        "sticker_reply_probability": settings.sticker_reply_probability,
        "voice_window_min_minutes": settings.voice_window_min_minutes,
        "voice_window_max_minutes": settings.voice_window_max_minutes,
        **_pacing_context(settings),
        "active_page": "features",
        "success": "能力开关已保存并生效",
    })
