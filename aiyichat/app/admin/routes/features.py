"""Admin 功能实验室路由。

集中管理对话引擎中"可灰度"的能力开关（默认关闭或需依赖外部密钥的能力），
以可视化 Toggle 的形式展示，保存后热重载生效。

与 multimedia 配置共享同一套持久化机制（AppSettingsRepository.upsert + settings.reload），
但仅聚焦布尔类能力开关，降低运营心智负担。
"""

from dataclasses import dataclass

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
              "仅统计真实用户消息，主动关怀不重置空闲计时。",
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
        desc="自动识别用户提及的未来事件（面试/考试等），到期后主动关心进展。",
        dependency="依赖焦点抽取（长期记忆）正常工作",
        cost="llm",
    ),
    FeatureToggle(
        key="embedding_enabled",
        label="语义记忆检索",
        desc="将长期记忆向量化，按语义相似度检索注入，显著提升'记得住'的体验。",
        dependency="需在「AI 模型配置」中为 embedding 功能配置 OpenAI 兼容的向量化服务（如百炼、OpenAI、硅基流动等）",
        cost="dashscope",
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
        "理解用户发表的表情包含义，并支持以图回图斗图。",
        dependency="需表情库非空（随包预置资源自动入库，可在「表情包管理」页上传补充）；"
        "开启语义检索时匹配更精准",
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
        desc="根据用户情绪与角色心情动态生成语音指令/情感标签/音调修饰，让语音回复更拟人化；"
        "关闭后语音无任何表现力修饰。",
        dependency="需先在「AI 模型配置」开启 TTS 语音合成",
        cost="free",
    ),
    FeatureToggle(
        key="voice_dynamic_enabled",
        label="语音动态触发窗口",
        desc="用户发语音触发语音回复后，该会话在随机时长的固定窗口内，"
        "文字消息也以语音回复；窗口到期自动回落为仅语音触发。"
        "窗口时长在下方「语音动态触发参数」中配置。",
        dependency="需先在「AI 模型配置」开启 TTS 语音合成",
        cost="free",
    ),
]


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
        "tz_offset": settings.default_timezone_offset,
        # 表情包触发参数
        "sticker_casual_probability": settings.sticker_casual_probability,
        "sticker_reply_probability": settings.sticker_reply_probability,
        # 语音动态触发窗口参数
        "voice_window_min_minutes": settings.voice_window_min_minutes,
        "voice_window_max_minutes": settings.voice_window_max_minutes,
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
    updates["default_timezone_offset"] = tz
    # 弃用字段同步写入，保障旧版本回滚不丢时区配置
    updates["chat_timezone_offset"] = tz
    updates["proactive_timezone_offset"] = tz

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
        "tz_offset": settings.default_timezone_offset,
        "sticker_casual_probability": settings.sticker_casual_probability,
        "sticker_reply_probability": settings.sticker_reply_probability,
        "voice_window_min_minutes": settings.voice_window_min_minutes,
        "voice_window_max_minutes": settings.voice_window_max_minutes,
        "active_page": "features",
        "success": "能力开关已保存并生效",
    })
