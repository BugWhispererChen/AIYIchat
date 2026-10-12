"""应用运行时设置的数据库模型。"""

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# 迁移生成的默认基础配置组的稳定 id
TEXT_DEFAULT_CONFIG_ID = "text-default"
DASHSCOPE_DEFAULT_CONFIG_ID = "dashscope-default"
IMAGEGEN_DEFAULT_CONFIG_ID = "imagegen-default"
TTS_DEFAULT_CONFIG_ID = "tts-default"
EMBEDDING_DEFAULT_CONFIG_ID = "embedding-default"
IMAGEGEN_EDIT_DEFAULT_CONFIG_ID = "imagegen-edit-default"

# .env 模板中的占位密钥：生成默认配置组时视为未配置
PLACEHOLDER_API_KEY = "sk-your-api-key-here"

# 默认基础配置组的 OpenAI 兼容端点（阿里云百炼）
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# 默认基础配置组：未配置任何 AI 模型时按此生成（api_key 留空，由用户填入）
DEFAULT_MODEL_GROUPS: tuple[dict[str, str], ...] = (
    {
        "id": TEXT_DEFAULT_CONFIG_ID,
        "name": "文本模型",
        "base_url": DEFAULT_BASE_URL,
        "api_key": "",
        "default_model": "qwen3.7-flash",
    },
    {
        "id": IMAGEGEN_DEFAULT_CONFIG_ID,
        "name": "文生图",
        "base_url": DEFAULT_BASE_URL,
        "api_key": "",
        "default_model": "qwen-image-3.0",
    },
    {
        "id": TTS_DEFAULT_CONFIG_ID,
        "name": "语音生成",
        "base_url": DEFAULT_BASE_URL,
        "api_key": "",
        "default_model": "qwen-audio-3.0-tts-plus",
    },
    {
        "id": EMBEDDING_DEFAULT_CONFIG_ID,
        "name": "向量模型",
        "base_url": DEFAULT_BASE_URL,
        "api_key": "",
        "default_model": "qwen3.7-text-embedding",
    },
    {
        "id": IMAGEGEN_EDIT_DEFAULT_CONFIG_ID,
        "name": "图片修改",
        "base_url": DEFAULT_BASE_URL,
        "api_key": "",
        "default_model": "qwen-image-edit",
    },
)

# 默认配置组的功能引用（文本类功能默认已指向 text-default，无需重复）
DEFAULT_CONFIG_REFS: dict[str, str] = {
    "imagegen_config_id": IMAGEGEN_DEFAULT_CONFIG_ID,
    "tts_config_id": TTS_DEFAULT_CONFIG_ID,
    "embedding_config_id": EMBEDDING_DEFAULT_CONFIG_ID,
    "imagegen_edit_config_id": IMAGEGEN_EDIT_DEFAULT_CONFIG_ID,
    "vision_config_id": TEXT_DEFAULT_CONFIG_ID,
}


class ModelConfig(BaseModel):
    """一组可复用的基础配置（连接凭证 + 默认模型），供各 AI 功能引用。"""

    id: str = ""
    name: str = ""
    base_url: str = ""  # OpenAI 兼容端点；dashscope SDK 类（ASR/TTS/ImageGen）可留空
    api_key: str = ""
    default_model: str = ""  # 该组下所有功能使用的模型名称


class AppSettingsDocument(BaseModel):
    """存储在 MongoDB app_settings 集合中的单文档配置。"""

    id: str = Field(default="default", alias="_id")

    # ── 基础配置：可添加多组连接凭证，供各功能引用 ──────────
    model_configs: list[ModelConfig] = Field(default_factory=list)

    # ── 各 AI 功能的模型引用 ──────────────────────────────
    # 每个功能通过 {task}_config_id 指向 model_configs 中的一组；
    # 各功能直接使用所引用配置组的 default_model。
    # 文本类附带 temperature/max_tokens/timeout_seconds 调优参数。
    # 对话主回复
    chat_config_id: str = TEXT_DEFAULT_CONFIG_ID
    chat_temperature: float = 0.85
    chat_max_tokens: int = 2048
    chat_timeout_seconds: int = 15
    # 主动消息
    proactive_config_id: str = TEXT_DEFAULT_CONFIG_ID
    proactive_temperature: float = 0.92
    proactive_max_tokens: int = 512
    proactive_timeout_seconds: int = 30
    # 焦点抽取（长期记忆）
    focus_config_id: str = TEXT_DEFAULT_CONFIG_ID
    focus_temperature: float = 0.0
    focus_max_tokens: int = 128
    focus_timeout_seconds: int = 20
    # 情绪识别
    emotion_config_id: str = TEXT_DEFAULT_CONFIG_ID
    emotion_temperature: float = 0.0
    emotion_max_tokens: int = 128
    emotion_timeout_seconds: int = 20
    # 上下文摘要
    summary_config_id: str = TEXT_DEFAULT_CONFIG_ID
    summary_temperature: float = 0.3
    summary_max_tokens: int = 1024
    summary_timeout_seconds: int = 30
    # 聊天记录导入分析
    chat_import_config_id: str = TEXT_DEFAULT_CONFIG_ID
    chat_import_temperature: float = 0.7
    chat_import_max_tokens: int = 2048
    chat_import_timeout_seconds: int = 60

    # ── 弃用字段（仅保留供迁移读取，运行时不再使用）────────
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_temperature: float = 0.85
    llm_max_tokens: int = 1024
    llm_timeout_seconds: int = 15
    # 辅助任务模型（弃用，迁移时用于生成 aux-default 组）
    llm_aux_base_url: str = ""
    llm_aux_api_key: str = ""
    llm_aux_model: str = ""

    # QQ Bot (legacy 单Bot模式)
    qq_app_id: str = ""
    qq_app_secret: str = ""
    qq_bot_token: str = ""
    qq_sandbox_mode: bool = True

    # 对话
    context_window_size: int = 20
    summary_trigger_count: int = 40

    # 限频
    rate_limit_per_user: int = 30

    # 管理后台
    admin_username: str = "admin"
    admin_password: str = ""
    admin_session_hours: int = 24

    # 阿里云百炼
    dashscope_api_key: str = ""

    # ASR
    asr_enabled: bool = False
    asr_config_id: str = DASHSCOPE_DEFAULT_CONFIG_ID

    # Vision
    vision_enabled: bool = False
    vision_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    vision_config_id: str = DASHSCOPE_DEFAULT_CONFIG_ID

    # TTS
    tts_enabled: bool = False
    tts_config_id: str = DASHSCOPE_DEFAULT_CONFIG_ID
    # TTS 表现力：情感指令/情感标签/音调联动开关（语速内嵌于指令文案）
    tts_expression_enabled: bool = True
    # 语音动态触发窗口：用户发语音触发语音回复后，该会话在随机时长的
    # 固定窗口内文字消息也以语音回复（按会话独立，窗口内不续期）
    voice_dynamic_enabled: bool = True
    voice_window_min_minutes: int = 5
    voice_window_max_minutes: int = 30

    # ImageGen
    imagegen_enabled: bool = False
    imagegen_config_id: str = DASHSCOPE_DEFAULT_CONFIG_ID

    # ImageGen 参考图编辑（人物一致性：定妆照 + 编辑指令）
    imagegen_edit_enabled: bool = False
    imagegen_edit_config_id: str = DASHSCOPE_DEFAULT_CONFIG_ID

    # 文本向量化（长期记忆语义检索/去重）
    embedding_enabled: bool = False
    embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_config_id: str = DASHSCOPE_DEFAULT_CONFIG_ID

    # 记忆语义检索
    memory_retrieve_topk: int = 8
    memory_semantic_weight: float = 0.6
    # 记忆双时间轴（现实成立轴 valid_at/invalid_at + 系统录入轴 expired_at）
    memory_fact_validity_enabled: bool = True  # 冲突裁决 UPDATE 时软失效旧事实而不删除
    memory_expired_filter_enabled: bool = True  # 注入前硬过滤已失效事实（仅作用于新字段非空者）

    # 人际关系网络（实体关系图谱增强）
    memory_graph_enabled: bool = False  # Kuzu 图数据库增强开关（未启用时降级 MongoDB 递归）
    memory_graph_path: str = "./data/entity_graph"  # Kuzu 数据文件路径

    # 情感支持
    emotion_llm_fallback_enabled: bool = False
    support_strategy_enabled: bool = True  # 纯规则零成本，默认启用
    character_mood_enabled: bool = True  # 角色心情系统：角色拥有自己的情绪状态并随对话演化
    extended_emotion_enabled: bool = True  # 扩展情绪检测开关（默认启用，支持 tired/excited/confused/nostalgic/grateful/shy）

    # 对话时间感知（注入当前时间 + 间隔处打时间标签，防止 LLM 把旧消息当作刚才）
    message_time_enabled: bool = True
    message_time_gap_minutes: int = 30  # 两条消息间隔超过该阈值才打标签（分钟，0=每条都打）
    # 单一时区源：对话时间感知、主动关怀免打扰、摘要日期前缀统一取该 UTC 偏移（小时）
    default_timezone_offset: int = 8
    # 时间感知子开关（各自独立生效，互不依赖；总开关关闭时全部不注入）
    time_anchor_enabled: bool = True  # 日期锚点：system prompt 开头声明"今天是几号"
    time_idle_hint_enabled: bool = True  # 间隔语义：告知距上一条用户消息已过去多久
    time_day_separator_enabled: bool = True  # 跨天分隔符：自然日变化处插入独立 system 消息
    message_time_tag_year_enabled: bool = True  # 跨年标签带年份（[2025-12-31 23:45]）
    memory_inject_time_suffix: bool = True  # 记忆注入追加"（记录于 N 天前）"后缀
    # jionlp 相对时间归一化（依赖缺失/导入失败时自动降级为纯 LLM，不阻断主链路）
    time_jionlp_enabled: bool = True
    # 弃用：时区统一由 default_timezone_offset 接管，仅保留供迁移脚本读取存量值
    chat_timezone_offset: int = 8

    # 上下文 token 预算
    context_token_budget: int = 3000

    # 人设一致性自检
    consistency_check_enabled: bool = True  # 纯规则零成本，默认启用

    # 回复抖动检测（拦截模型复读上一条回复：命中后追加提示重试一次）
    reply_jitter_detection_enabled: bool = True  # 复读检测总开关
    reply_jitter_similarity: float = 0.9  # 相似度阈值（0~1，达到即判定为复读）

    # 事件驱动主动对话
    proactive_event_enabled: bool = False

    # 主动关怀总开关与安全阈值
    proactive_enabled: bool = True  # 主动关怀总开关，异常时可一键停用
    proactive_max_retries: int = 2  # 单阶段失败重试上限，达到后不再触发（防额度耗尽）
    proactive_monthly_limit: int = 4  # 每用户每月主动消息上限（QQ 官方约束为4条）

    # 主动关怀免打扰时段（用户本地时区，跨午夜：start~24 与 0~end）
    proactive_quiet_hour_start: int = 22
    proactive_quiet_hour_end: int = 8
    # 弃用：时区统一由 default_timezone_offset 接管，仅保留供迁移脚本读取存量值
    proactive_timezone_offset: int = 8

    # 分享型主动消息（时事/新闻/见闻/趣事）：召回触发点按概率替代关怀型内容，
    # 不新增发送频次，免打扰/月度配额/通道预检/重试熔断全部复用
    proactive_share_enabled: bool = True  # 分享型消息开关
    proactive_share_ratio: float = 0.5  # 触发时选"分享型"的概率（0~1，其余走原阶段关怀）
    proactive_share_news_ratio: float = 0.5  # 分享型中"联网时事"占比（其余为"创作趣事"）

    # 聊天记录导入：分块大小（每块目标消息数）
    chat_import_chunk_size: int = 100
    # 导入记录时间戳所属时区的 UTC 偏移（小时）：导出文件为本地墙钟时间，
    # 不按该偏移转 aware UTC 会被当作 UTC 再渲染，整体错位 8 小时
    chat_import_timezone_offset: int = 8

    # Function Calling
    function_calling_enabled: bool = False

    # 内容安全：运营追加词表（与内置词库合并生效，支持热更新）
    safety_crisis_extra: list[str] = Field(default_factory=list)
    safety_forbidden_extra: list[str] = Field(default_factory=list)

    # 表情包（AI 主动发送/理解用户表情/以图回图；语义检索 + 关键词兜底）
    sticker_enabled: bool = False  # 表情包发送总开关（需表情库非空）
    sticker_seed_enabled: bool = True  # 启动时自动 seed 随包预置的表情包资源
    sticker_max_per_turn: int = 1  # 每轮回复最多发送的表情包数量
    sticker_similarity_threshold: float = 0.28  # 语义检索相似度门槛（低于视为未命中）
    sticker_reply_probability: float = 0.5  # 用户仅发表情包时以图回图的概率（0~1）
    sticker_casual_probability: float = 0.15  # 普通聊天时概率性穿插表情包的概率（0~1）

    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = ConfigDict(
        populate_by_name=True,
    )

    @property
    def to_flat_dict(self) -> dict[str, Any]:
        """返回扁平化的字段字典（排除 id 和元数据）。"""
        data = self.model_dump(by_alias=False)
        data.pop("id", None)
        data.pop("updated_at", None)
        return data

    @classmethod
    def from_mongo(cls, doc: dict | None) -> "AppSettingsDocument | None":
        if doc is None:
            return None
        doc["_id"] = str(doc["_id"])
        return cls(**doc)
