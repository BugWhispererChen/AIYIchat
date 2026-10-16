import logging
import os
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)

# 不可热重载的基础设施字段
_NO_RELOAD_FIELDS = frozenset({
    "mongodb_uri", "mongodb_database", "use_mock_db",
    "app_env", "log_level",
})


class _BootstrapSettings(BaseSettings):
    """仅从 .env / 环境变量读取的启动配置，用于连接数据库。"""

    app_env: str = Field(default="development")
    log_level: str = Field(default="INFO")
    mongodb_uri: str = Field(default="mongodb://localhost:27017")
    mongodb_database: str = Field(default="aiyichat")
    use_mock_db: bool = Field(default=False)

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


_bootstrap = _BootstrapSettings()


class _RuntimeSettings:
    """运行时配置容器，支持从数据库或 .env 热重载。"""

    # 基础设施字段（不可热重载，从 _bootstrap 同步）
    app_env: str = "development"
    log_level: str = "INFO"
    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_database: str = "aiyichat"
    use_mock_db: bool = False

    # 基础配置：可复用的连接凭证组（名称+base_url+api_key+默认model）
    model_configs: list = []

    # 各 AI 功能的模型引用：{task}_config_id 指向 model_configs 中一组，
    # 各功能直接使用所引用配置组的 default_model；文本类附带 temperature/max_tokens/timeout。
    chat_config_id: str = "text-default"
    chat_temperature: float = 0.85
    chat_max_tokens: int = 2048
    chat_timeout_seconds: int = 15
    chat_presence_penalty: float = 0.3  # 采样层防重复：正值鼓励新话题/新表达（0~2）
    proactive_config_id: str = "text-default"
    proactive_temperature: float = 0.92
    proactive_max_tokens: int = 512
    proactive_timeout_seconds: int = 30
    focus_config_id: str = "text-default"
    focus_temperature: float = 0.0
    focus_max_tokens: int = 128
    focus_timeout_seconds: int = 20
    emotion_config_id: str = "text-default"
    emotion_temperature: float = 0.0
    emotion_max_tokens: int = 128
    emotion_timeout_seconds: int = 20
    summary_config_id: str = "text-default"
    summary_temperature: float = 0.3
    summary_max_tokens: int = 1024
    summary_timeout_seconds: int = 30
    chat_import_config_id: str = "text-default"
    chat_import_temperature: float = 0.7
    chat_import_max_tokens: int = 2048
    chat_import_timeout_seconds: int = 60

    # 对话
    context_window_size: int = 20
    summary_trigger_count: int = 40

    # 长期记忆
    memory_max_facts: int = 30  # 每个服务最大记忆事实数
    memory_extract_interval: int = 6  # 每隔多少条消息提取一次焦点（默认6=3轮对话）
    memory_inject_limit: int = 10  # prompt中最多注入多少条记忆事实

    # 上下文管理
    summary_max_length: int = 200  # 摘要最大字符数

    # 话题管理
    topic_history_max: int = 10  # 话题历史最大保存数
    topic_stall_detection_enabled: bool = True  # 话题停滞检测开关
    topic_stall_threshold: int = 4  # 连续 N 轮同话题判定为停滞
    topic_stall_similarity: float = 0.6  # 用户输入相似度阈值（低于此值视为同话题）

    # 拟人化多消息回复
    multi_message_enabled: bool = True  # 多消息回复总开关
    multi_message_max_count: int = 5  # 单次回复最大消息条数
    multi_message_delay_per_char: float = 0.05  # 模拟打字延迟：每字延迟秒数
    multi_message_max_delay: float = 2.0  # 相邻消息间最大延迟秒数

    # 回复抖动检测（拦截模型复读上一条回复：命中后追加提示重试）
    reply_jitter_detection_enabled: bool = True  # 复读检测总开关
    reply_jitter_similarity: float = 0.9  # 相似度阈值（0~1，达到即判定为复读）
    reply_jitter_similarity_warn: float = 0.75  # 轻微重复警告阈值（0~1，达到即追加提示重试但不递增温度）
    reply_jitter_window: int = 4  # 比对最近 N 条 assistant 消息（默认 4）
    reply_jitter_retry_max: int = 2  # 最大重试次数（默认 2）
    reply_jitter_retry_temp_step: float = 0.15  # 每次重试温度递增步长

    # 对话时间感知（防止 LLM 把旧消息当作刚才：注入当前时间 + 间隔处打时间标签）
    message_time_enabled: bool = True  # 时间感知总开关
    message_time_gap_minutes: int = 30  # 两条消息间隔超过该阈值才打标签（分钟，0=每条都打）
    time_session_gap_hours: int = 8  # 会话间隔软档阈值（小时）
    time_reunion_gap_hours: int = 72  # 久别重逢强档阈值（小时）
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

    # 主动消息
    proactive_llm_temperature: float = 0.92  # 主动消息LLM温度

    # 管理后台
    admin_username: str = "admin"
    admin_password: str = ""
    admin_session_hours: int = 24

    # 阿里云百炼
    dashscope_api_key: str = ""

    # ASR
    asr_enabled: bool = False
    asr_config_id: str = "dashscope-default"

    # Vision
    vision_enabled: bool = False
    vision_config_id: str = "dashscope-default"

    # TTS
    tts_enabled: bool = False
    tts_config_id: str = "dashscope-default"
    # TTS 表现力：情感指令/情感标签/音调联动（关闭时语音无任何表现力修饰）
    tts_expression_enabled: bool = True
    # 语音动态触发窗口：用户发语音触发语音回复后，该会话在随机时长的
    # 固定窗口内文字消息也以语音回复（按会话独立，窗口内不续期）
    voice_dynamic_enabled: bool = True
    voice_window_min_minutes: int = 5
    voice_window_max_minutes: int = 30

    # ImageGen
    imagegen_enabled: bool = False
    imagegen_config_id: str = "dashscope-default"

    # ImageGen 参考图编辑（人物一致性：定妆照 + 编辑指令）
    imagegen_edit_enabled: bool = False
    imagegen_edit_config_id: str = "dashscope-default"

    # 文本向量化（长期记忆语义检索/去重；关闭或无密钥时自动降级到规则逻辑）
    embedding_enabled: bool = False
    embedding_config_id: str = "dashscope-default"

    # 记忆语义检索
    memory_retrieve_topk: int = 8  # 语义检索注入的记忆条数
    memory_semantic_weight: float = 0.6  # 语义相似度权重（其余归时效×类别）

    # 记忆双时间轴（现实成立轴 valid_at/invalid_at + 系统录入轴 expired_at）
    memory_fact_validity_enabled: bool = True  # 冲突裁决 UPDATE 时软失效旧事实而不删除
    memory_expired_filter_enabled: bool = True  # 注入前硬过滤已失效事实（仅作用于新字段非空者）

    # 人际关系网络（实体关系图谱增强）
    memory_max_entities: int = 50  # 每个服务最大实体数（超限按活跃度淘汰）
    memory_entity_scan_turns: int = 3  # 实体命中窗口：扫描最近 N 轮历史消息
    memory_entity_merge_threshold: float = 0.75  # 实体消歧合并的 embedding 相似度阈值
    memory_graph_enabled: bool = False  # Kuzu 图数据库增强开关（未启用时降级 MongoDB 递归）
    memory_graph_path: str = "./data/entity_graph"  # Kuzu 数据文件路径

    # 情感支持
    emotion_llm_fallback_enabled: bool = False  # 关键词未命中时降级LLM情绪识别
    support_strategy_enabled: bool = True  # 情感支持策略层（探索→安慰→行动）；纯规则零成本，默认启用
    character_mood_enabled: bool = True  # 角色心情系统：角色拥有自己的情绪状态并随对话演化
    extended_emotion_enabled: bool = True  # 扩展情绪检测开关（默认启用，支持 tired/excited/confused/nostalgic/grateful/shy）

    # 上下文 token 预算（历史+摘要注入上限）
    context_token_budget: int = 3000

    # 人设一致性自检
    consistency_check_enabled: bool = True  # 纯规则零成本，默认启用（防人设穿帮+合规）

    # 事件驱动主动对话
    proactive_event_enabled: bool = False

    # 主动关怀总开关与安全阈值
    proactive_enabled: bool = True  # 主动关怀总开关，异常时可一键停用
    proactive_max_retries: int = 2  # 单阶段失败重试上限，达到后不再触发（防额度耗尽）
    proactive_monthly_limit: int = 4  # 每用户每月主动消息上限（QQ 官方约束为4条）

    # 主动关怀免打扰时段（用户本地时区，跨午夜：start~24 与 0~end）
    proactive_quiet_hour_start: int = 22
    proactive_quiet_hour_end: int = 8

    # 分享型主动消息（时事/新闻/见闻/趣事）：在召回触发点按概率替代关怀型内容，
    # 不新增发送频次，免打扰/月度配额/通道预检/重试熔断全部复用
    proactive_share_enabled: bool = True  # 分享型消息开关
    proactive_share_ratio: float = 0.5  # 触发时选"分享型"的概率（0~1，其余走原阶段关怀）
    proactive_share_news_ratio: float = 0.5  # 分享型中"联网时事"占比（其余为"创作趣事"）

    # 聊天记录导入：分块大小（每块目标消息数）
    chat_import_chunk_size: int = 100
    # 导入记录时间戳所属时区的 UTC 偏移（小时），修正存量导入整体错位 8 小时的问题
    chat_import_timezone_offset: int = 8

    # Function Calling（默认关闭，谨慎灰度）
    function_calling_enabled: bool = False

    # 内容安全：运营追加词表（与内置词库合并，变更后重建 SafetyChecker）
    safety_crisis_extra: list = []
    safety_forbidden_extra: list = []

    # 表情包（AI 主动发送/理解用户表情/以图回图；语义检索 + 关键词兜底）
    sticker_enabled: bool = False  # 表情包发送总开关（需表情库非空）
    sticker_seed_enabled: bool = True  # 启动时自动 seed 随包预置的表情包资源
    sticker_max_per_turn: int = 1  # 每轮回复最多发送的表情包数量
    sticker_similarity_threshold: float = 0.28  # 语义检索相似度门槛（低于视为未命中）
    sticker_reply_probability: float = 0.5  # 用户仅发表情包时以图回图的概率（0~1）
    sticker_casual_probability: float = 0.15  # 普通聊天时概率性穿插表情包的概率（0~1）
    sticker_max_dimension: int = 500  # 表情包尺寸阈值（宽或高中较大者，像素）

    def __init__(self) -> None:
        # 基础设施字段不可热重载，直接从 _bootstrap 同步
        self.app_env = _bootstrap.app_env
        self.log_level = _bootstrap.log_level
        self.mongodb_uri = _bootstrap.mongodb_uri
        self.mongodb_database = _bootstrap.mongodb_database
        self.use_mock_db = _bootstrap.use_mock_db
        # 初始从 .env 加载一次（通过 pydantic 隐式读取）
        self._apply_dict(_bootstrap_settings_to_dict())

    def _apply_dict(self, data: dict[str, Any]) -> set[str]:
        """将字典值应用到当前实例，返回变更字段集合。"""
        changed: set[str] = set()
        for name in self._fields():
            if name in _NO_RELOAD_FIELDS:
                continue
            new_val = data.get(name)
            if new_val is None:
                continue
            old_val = getattr(self, name)
            if old_val != new_val:
                object.__setattr__(self, name, new_val)
                changed.add(name)
        return changed

    async def reload(self) -> set[str]:
        """
        热重载: 优先从数据库读取，失败则回退到 .env。
        Returns 变更的字段名集合。
        """
        changed: set[str] = set()
        try:
            from app.db.repositories.app_settings_repo import AppSettingsRepository
            repo = AppSettingsRepository()

            doc = await repo.get_document()
            if doc:
                changed = self._apply_dict(doc.to_flat_dict)
                self._sync_to_environ()
                logger.info("Settings reloaded from database: %d field(s) changed", len(changed))
                return changed
        except Exception as e:
            logger.warning("Failed to reload settings from database: %s, falling back to .env", e)

        # fallback: 从 .env 重新读取
        changed = self._apply_dict(_bootstrap_settings_to_dict())
        self._sync_to_environ()
        if changed:
            logger.info("Settings reloaded from .env: %d field(s) changed", len(changed))
        return changed

    def _sync_to_environ(self) -> None:
        """将当前值同步到 os.environ，保证兼容性。"""
        for name in self._fields():
            if name in _NO_RELOAD_FIELDS:
                continue
            val = getattr(self, name)
            # model_configs 为结构化列表，不参与环境变量同步
            if isinstance(val, (list, dict)):
                continue
            os.environ[name.upper()] = str(val) if not isinstance(val, bool) else ("true" if val else "false")

    @classmethod
    def _fields(cls) -> set[str]:
        return {
            "app_env", "log_level", "mongodb_uri", "mongodb_database", "use_mock_db",
            "model_configs",
            "chat_config_id", "chat_temperature",
            "chat_max_tokens", "chat_timeout_seconds", "chat_presence_penalty",
            "proactive_config_id", "proactive_temperature",
            "proactive_max_tokens", "proactive_timeout_seconds",
            "focus_config_id", "focus_temperature",
            "focus_max_tokens", "focus_timeout_seconds",
            "emotion_config_id", "emotion_temperature",
            "emotion_max_tokens", "emotion_timeout_seconds",
            "summary_config_id", "summary_temperature",
            "summary_max_tokens", "summary_timeout_seconds",
            "chat_import_config_id", "chat_import_temperature",
            "chat_import_max_tokens", "chat_import_timeout_seconds",
            "context_window_size", "summary_trigger_count",
            "memory_max_facts", "memory_extract_interval", "memory_inject_limit",
            "summary_max_length", "topic_history_max", "proactive_llm_temperature",
            "topic_stall_detection_enabled", "topic_stall_threshold",
            "topic_stall_similarity",
            "multi_message_enabled", "multi_message_max_count",
            "multi_message_delay_per_char", "multi_message_max_delay",
            "reply_jitter_detection_enabled", "reply_jitter_similarity",
            "reply_jitter_similarity_warn",
            "reply_jitter_window", "reply_jitter_retry_max",
            "reply_jitter_retry_temp_step",
            "message_time_enabled", "message_time_gap_minutes",
            "time_session_gap_hours", "time_reunion_gap_hours",
            "default_timezone_offset", "time_anchor_enabled", "time_idle_hint_enabled",
            "time_day_separator_enabled", "message_time_tag_year_enabled",
            "memory_inject_time_suffix", "time_jionlp_enabled",
            "admin_username", "admin_password", "admin_session_hours",
            "dashscope_api_key",
            "asr_enabled", "asr_config_id",
            "vision_enabled", "vision_config_id",
            "tts_enabled", "tts_config_id", "tts_expression_enabled",
            "voice_dynamic_enabled", "voice_window_min_minutes",
            "voice_window_max_minutes",
            "imagegen_enabled", "imagegen_config_id",
            "imagegen_edit_enabled", "imagegen_edit_config_id",
            "embedding_enabled", "embedding_config_id",
            "memory_retrieve_topk", "memory_semantic_weight",
            "memory_fact_validity_enabled", "memory_expired_filter_enabled",
            "memory_max_entities", "memory_entity_scan_turns",
            "memory_entity_merge_threshold", "memory_graph_enabled", "memory_graph_path",
            "emotion_llm_fallback_enabled", "support_strategy_enabled",
            "character_mood_enabled", "extended_emotion_enabled",
            "context_token_budget", "consistency_check_enabled",
            "proactive_event_enabled", "function_calling_enabled",
            "proactive_enabled", "proactive_max_retries",
            "proactive_monthly_limit",
            "proactive_quiet_hour_start", "proactive_quiet_hour_end",
            "proactive_share_enabled", "proactive_share_ratio",
            "proactive_share_news_ratio",
            "chat_import_chunk_size",
            "chat_import_timezone_offset",
            "safety_crisis_extra", "safety_forbidden_extra",
            "sticker_enabled", "sticker_seed_enabled", "sticker_max_per_turn",
            "sticker_similarity_threshold", "sticker_reply_probability",
            "sticker_casual_probability", "sticker_max_dimension",
        }


def _bootstrap_settings_to_dict() -> dict[str, Any]:
    """用 pydantic BaseSettings 读取一次完整的 .env，返回扁平 dict。"""
    # 临时构造一个完整的 BaseSettings 来解析 .env
    class _FullEnvSettings(BaseSettings):
        app_env: str = Field(default="development")
        log_level: str = Field(default="INFO")
        mongodb_uri: str = Field(default="mongodb://localhost:27017")
        mongodb_database: str = Field(default="aiyichat")
        use_mock_db: bool = Field(default=False)
        model_configs: list = Field(default_factory=list)
        chat_config_id: str = Field(default="text-default")
        chat_temperature: float = Field(default=0.85)
        chat_max_tokens: int = Field(default=2048)
        chat_timeout_seconds: int = Field(default=15)
        chat_presence_penalty: float = Field(default=0.3)
        proactive_config_id: str = Field(default="text-default")
        proactive_temperature: float = Field(default=0.92)
        proactive_max_tokens: int = Field(default=512)
        proactive_timeout_seconds: int = Field(default=30)
        focus_config_id: str = Field(default="text-default")
        focus_temperature: float = Field(default=0.0)
        focus_max_tokens: int = Field(default=128)
        focus_timeout_seconds: int = Field(default=20)
        emotion_config_id: str = Field(default="text-default")
        emotion_temperature: float = Field(default=0.0)
        emotion_max_tokens: int = Field(default=128)
        emotion_timeout_seconds: int = Field(default=20)
        summary_config_id: str = Field(default="text-default")
        summary_temperature: float = Field(default=0.3)
        summary_max_tokens: int = Field(default=1024)
        summary_timeout_seconds: int = Field(default=30)
        chat_import_config_id: str = Field(default="text-default")
        chat_import_temperature: float = Field(default=0.7)
        chat_import_max_tokens: int = Field(default=2048)
        chat_import_timeout_seconds: int = Field(default=60)
        context_window_size: int = Field(default=20)
        summary_trigger_count: int = Field(default=40)
        memory_max_facts: int = Field(default=30)
        memory_extract_interval: int = Field(default=6)
        memory_inject_limit: int = Field(default=10)
        summary_max_length: int = Field(default=200)
        topic_history_max: int = Field(default=10)
        topic_stall_detection_enabled: bool = Field(default=True)
        topic_stall_threshold: int = Field(default=4)
        topic_stall_similarity: float = Field(default=0.6)
        multi_message_enabled: bool = Field(default=True)
        multi_message_max_count: int = Field(default=5)
        multi_message_delay_per_char: float = Field(default=0.05)
        multi_message_max_delay: float = Field(default=2.0)
        reply_jitter_detection_enabled: bool = Field(default=True)
        reply_jitter_similarity: float = Field(default=0.9)
        reply_jitter_similarity_warn: float = Field(default=0.75)
        reply_jitter_window: int = Field(default=4)
        reply_jitter_retry_max: int = Field(default=2)
        reply_jitter_retry_temp_step: float = Field(default=0.15)
        message_time_enabled: bool = Field(default=True)
        message_time_gap_minutes: int = Field(default=30)
        time_session_gap_hours: int = Field(default=8)
        time_reunion_gap_hours: int = Field(default=72)
        default_timezone_offset: int = Field(default=8)
        time_anchor_enabled: bool = Field(default=True)
        time_idle_hint_enabled: bool = Field(default=True)
        time_day_separator_enabled: bool = Field(default=True)
        message_time_tag_year_enabled: bool = Field(default=True)
        memory_inject_time_suffix: bool = Field(default=True)
        time_jionlp_enabled: bool = Field(default=True)
        proactive_llm_temperature: float = Field(default=0.92)
        admin_username: str = Field(default="admin")
        admin_password: str = Field(default="")
        admin_session_hours: int = Field(default=24)
        dashscope_api_key: str = Field(default="")
        asr_enabled: bool = Field(default=False)
        asr_config_id: str = Field(default="dashscope-default")
        vision_enabled: bool = Field(default=False)
        vision_config_id: str = Field(default="dashscope-default")
        tts_enabled: bool = Field(default=False)
        tts_config_id: str = Field(default="dashscope-default")
        tts_expression_enabled: bool = Field(default=True)
        voice_dynamic_enabled: bool = Field(default=True)
        voice_window_min_minutes: int = Field(default=5)
        voice_window_max_minutes: int = Field(default=30)
        imagegen_enabled: bool = Field(default=False)
        imagegen_config_id: str = Field(default="dashscope-default")
        imagegen_edit_enabled: bool = Field(default=False)
        imagegen_edit_config_id: str = Field(default="dashscope-default")
        embedding_enabled: bool = Field(default=False)
        embedding_config_id: str = Field(default="dashscope-default")
        memory_retrieve_topk: int = Field(default=8)
        memory_semantic_weight: float = Field(default=0.6)
        memory_fact_validity_enabled: bool = Field(default=True)
        memory_expired_filter_enabled: bool = Field(default=True)
        memory_max_entities: int = Field(default=50)
        memory_entity_scan_turns: int = Field(default=3)
        memory_entity_merge_threshold: float = Field(default=0.75)
        memory_graph_enabled: bool = Field(default=False)
        memory_graph_path: str = Field(default="./data/entity_graph")
        emotion_llm_fallback_enabled: bool = Field(default=False)
        support_strategy_enabled: bool = Field(default=True)
        character_mood_enabled: bool = Field(default=True)
        extended_emotion_enabled: bool = Field(default=True)
        context_token_budget: int = Field(default=3000)
        consistency_check_enabled: bool = Field(default=True)
        proactive_event_enabled: bool = Field(default=False)
        function_calling_enabled: bool = Field(default=False)
        proactive_enabled: bool = Field(default=True)
        proactive_max_retries: int = Field(default=2)
        proactive_monthly_limit: int = Field(default=4)
        proactive_quiet_hour_start: int = Field(default=22)
        proactive_quiet_hour_end: int = Field(default=8)
        proactive_share_enabled: bool = Field(default=True)
        proactive_share_ratio: float = Field(default=0.5)
        proactive_share_news_ratio: float = Field(default=0.5)
        chat_import_chunk_size: int = Field(default=100)
        chat_import_timezone_offset: int = Field(default=8)
        safety_crisis_extra: list = Field(default_factory=list)
        safety_forbidden_extra: list = Field(default_factory=list)
        sticker_enabled: bool = Field(default=False)
        sticker_seed_enabled: bool = Field(default=True)
        sticker_max_per_turn: int = Field(default=1)
        sticker_similarity_threshold: float = Field(default=0.28)
        sticker_reply_probability: float = Field(default=0.5)
        sticker_casual_probability: float = Field(default=0.15)
        sticker_max_dimension: int = Field(default=500)
        # 忽略 .env 中未声明的键（如 DEPLOY_* 部署凭据），与 _BootstrapSettings 保持一致
        model_config = {
            "env_file": ".env",
            "env_file_encoding": "utf-8",
            "extra": "ignore",
        }

    inst = _FullEnvSettings()
    return {k: getattr(inst, k) for k in type(inst).model_fields}


# 导出兼容旧代码的接口
settings = _RuntimeSettings()


async def load_settings_from_db() -> set[str]:
    """
    从 MongoDB 加载运行时配置。
    若数据库为空，则自动从 .env seed 到数据库后再加载。
    返回变更的字段集合。
    """
    from app.db.repositories.app_settings_repo import AppSettingsRepository

    repo = AppSettingsRepository()
    seeded = await repo.seed_from_env(".env")
    if seeded:
        logger.info("App settings seeded from .env to database")

    # 迁移：为缺失基础配置组的存量文档补齐（幂等）
    migrated = await repo.migrate_model_configs_if_needed()
    if migrated:
        logger.info("App settings model_configs 已补齐默认分组")

    doc = await repo.get_document()
    if doc is None:
        logger.warning("No app_settings document found in database, using .env values")
        changed = settings._apply_dict(_bootstrap_settings_to_dict())
        settings._sync_to_environ()
        return changed

    changed = settings._apply_dict(doc.to_flat_dict)
    settings._sync_to_environ()
    logger.info("App settings loaded from database: %d field(s) changed", len(changed))
    return changed
