# 技术架构（AIYIchat）

> 本文面向**想读源码、要改代码**的贡献者。
> 只想知道它能做什么、怎么装，请看 [README](../README.md) 与
> [部署包说明](../aiyichat/README.md)。

以下所有模块名与目录均以仓库实际代码为准（`aiyichat/app/`）；每个模块文件顶部有一段中文职责说明，
读代码时优先看那段。

---

## 1. 技术栈

| 层 | 选型 | 说明 |
|---|---|---|
| 语言 / 运行时 | Python 3.11 | 全异步（`asyncio`），Windows 侧另有 Tk 面板 |
| Web 框架 | FastAPI + Uvicorn | 同时承载管理后台、C 端网页与开放 API |
| 数据库 | MongoDB（Motor 异步驱动） | Repository 模式收口在 `app/db/repositories/` |
| 可选增强 | Kuzu 嵌入式图数据库 | 人际关系图谱，未启用时降级为 Mongo 一跳查询 |
| 管理后台 | Jinja2 + HTMX + PicoCSS | 服务端渲染 + 局部增量更新，无前端构建链 |
| 模型接入 | OpenAI 兼容接口 | 阿里云百炼 / DeepSeek 等，多凭证、配置热重载 |
| 交付形态 | Docker Compose / Windows EXE | 见第 6 节 |

---

## 2. 一条对话链路

所有渠道共用同一条链路，平台差异只存在于最左边的适配器里：

```
对方消息
    ↓
QQ WebSocket / 微信 iLink      ← app/adapters/（平台差异收敛为统一消息格式）
    ↓
NormalizedMessage              ← 文本 + media_urls/media_kinds（多 item 不互相覆盖）
    ↓
app/conversation/engine.py     ← 按 (bot, platform, 会话) 串行整轮，避免乱序与重复建身份
    ↓
情绪与心境 → 角色解析 → 上下文与记忆检索/注入 → 提示词组装
    ↓                            （emotion_detector / mood_manager / profiles / context_manager
    ↓                             memory_hygiene / reflection / entity_graph / prompt_assembler）
LLM 调用                        ← app/llm/openai_provider.py（流式，思考模式按端点×模型族下发）
    ↓
清洗与放行                       ← stream_gate（按句缓冲，先清洗再放行）
    ↓                             ai_tells（中文 AI 腔门禁）/ reply_jitter（复读重生）
    ↓                             message_splitter + human_rhythm（分条与节奏）
    ↓                             response_processor（截断 / 安全过滤）/ safety_checker
    ↓
回发                            ← app/adapters/（文本 / 语音 / 图片 / 表情包）
    ↓
落库与计量                       ← turn_recorder / 配额扣减（失败退还）/ 媒体引用登记
```

C 端网页走同一引擎，出口换成 SSE 网关（`[MSG]` / `[RESET]` / `[MEDIA]` / `[USER_TEXT]` 控制帧），
并由会话令牌反查身份——客户端传入的 `platform_id` / `bot_id` 一律不信任。

---

## 3. 目录与职责

| 模块 | 职责 |
|---|---|
| `app/adapters/` | QQ / 微信渠道适配、消息归一化、回发与引用处理、渠道扫码绑定 |
| `app/conversation/` | 对话引擎与全部拟人环节：情绪、心境、话题、上下文、记忆治理与反思、流式闸门、AI 腔门禁、分条节奏、安全过滤 |
| `app/llm/` | Provider 抽象与工厂、提示词组装、凭证分组、思考模式与超时/重试口径 |
| `app/profiles/` | 角色档案定义、渲染为系统 Prompt、预置角色种子数据 |
| `app/services/` | ASR / TTS / Vision / ImageGen / 声音复刻 / 向量与嵌入 / 关系图谱 / 媒体存储 / 表情包 / 语音窗口 / 网页账号与渠道 / 服务装配 |
| `app/web/`、`app/api/` | C 端网页与开放 API（鉴权、流式对话、媒体、账号中心） |
| `app/admin/` | 管理后台（登录与会话、仪表盘、角色、账号与渠道、对话、记忆、多媒体、配额、功能实验室、网页账号） |
| `app/chat_import/` | 聊天记录导入与分块分析（角色风格克隆） |
| `app/proactive/` | 主动关怀调度：时机、勿扰、退让、时事 / 趣事分享 |
| `app/db/` | 连接与 Repository 层（含引用保护型删除、历史归档查询） |
| `app/models/` | 数据模型与校验 |
| `app/middleware/` | 限流、安全头、异常收口 |
| `app/utils/` | 口令哈希与强度判定、时间归一、文本与音频工具等 |

`tools/` 下是与运行时无关的工程件：`deploy_package/`（Linux 打包与校验）、
`windows_service/`（PyInstaller + Inno Setup 安装包与服务面板）、`eval/`（离线评测）、
`migrations/`（数据迁移，默认 dry-run）、`test/`（测试套件）。

---

## 4. 数据模型要点

- **身份（Identity）**：一个渠道里的一个对话方，承载记忆与配额。
- **账号（Account）**：C 端登录主体（`web_identities` / `web_sessions`），与身份**相互独立**；
  一个账号可有多个身份（跨渠道），凭据与会话独立存储，密码哈希不进入对话上下文。
- **服务（Service）**：「身份 × 渠道 × 角色」的对话装配单元，决定扮演谁、记忆与额度归属。
- **渠道（Channel）**：Bot 凭据的载体，QQ / 微信扫码绑定，凭据只存"是否已配置"，接口不回传明文。
- **记忆**：事实（含点状时效标记）、摘要、反思洞见（带来源事实引用）、实体与关系。
- **媒体**：不可枚举的能力地址 `/api/media/{public_id}`；账号级配额，共享资源受引用保护不被驱逐。
- **配额**：按文字（Token）/ 语音（次）/ 图片（张）分别计量，失败退还，主动消息发送前预检。

> 术语口径见 `aiyichat/TERMINOLOGY.md`（界面文案与叙述用词由测试用例守卫，避免漂移）。

---

## 5. 几个关键取舍

| 取舍 | 为什么这样做 |
|---|---|
| **流式先清洗再放行** | 清洗排在全文之后等于用户全看得到、只有落库是干净的。改为攒到句边界且确认无跨 token 半截标记才放行，代价是首字延迟约 0.3~1 秒 |
| **稳定记忆留前缀、本轮召回后置** | 前缀逐轮变化会击穿 prompt 缓存、响应显著变慢；分层注入同时保住"记得多"和"回得快" |
| **向量化统一超时预算 + 熔断（慢成功计入失败）** | 裸调用叠加 SDK 默认重试，一次端点长挂能吃掉一分多钟；超时只放弃本轮语义召回，绝不阻塞对话 |
| **拟人护栏用确定性规则而非 LLM 自检** | 靠提示词约束不可靠（模型照样写），靠模型自检要多花一次调用；正则门禁零模型成本、可单测验证误删边界 |
| **限流与验证码为单实例内存实现** | 换取"一台机器就能跑"；代价是多实例水平扩展时各实例独立计数会失效，需换 Redis（已在 README 的合规章节写明） |
| **纯源码交付（Linux 包）** | MIT 开源前提下，部署机不需要额外构建链，用户可直接阅读审计修改；打包校验反向确认包内不应出现 `.so` |
| **Windows 走自带依赖的 EXE** | 目标用户不装 Docker / Python / MongoDB：面板 + 数据库服务 + ffmpeg + 自签名证书一次装完，默认端口 8443 |

---

## 6. 部署形态

| 形态 | 入口 | 说明 |
|---|---|---|
| Docker Compose | `aiyichat/docker-compose.yml` | 源码 + 编排，`deploy.sh` / `deploy.ps1` 封装证书与端口 |
| Linux 部署包 | `aiyichat-deploy-<分支>-<日期>-linux.tar.gz` | 纯源码形态，由 `tools/deploy_package/` 产出，打包后自动做静态完整性与运行级可用性校验 |
| Windows 安装包 | `AIYIchat_Setup_<分支>_<日期>.exe` | Inno Setup，内含 PyInstaller 产物、MongoDB Community、ffmpeg；`tools/windows_service/` 产出 |

配置分两处：**`.env` 放基础设施**（MongoDB 连接、百炼 Key、端口、开关默认值），
**数据库放业务配置**（模型与凭证分组、渠道凭据、功能实验室参数、C 端策略）。
后台保存过的值优先于 `.env`，因此升级不会把已配置的部署覆盖回代码默认值。

---

## 7. 质量与可观测

- **测试**：`aiyichat/tools/test/`（含 SSE 分帧、C 端归属校验静态护栏、命名口径守卫等契约用例）。
- **静态检查**：ruff + mypy（CI 与本地同口径钉版本，避免"本地绿 CI 红"）。
- **离线评测**：`aiyichat/tools/eval/` —— 模型评审（LLM-as-a-Judge）、确定性指标（长度分布、
  AI 腔命中、记忆治理统计）、GSB 成对比较、角色克隆评测入口。
- **运行时**：系统指标看板、健康检查与熔断状态、错误聚合、流式分阶段耗时日志
  （`stage_ms=... pre_llm=... total=...`，客户端中止的轮次同样输出）。

---

## 8. 延伸阅读

- [README](../README.md) —— 能力与亮点、快速开始
- [aiyichat/README.md](../aiyichat/README.md) —— 部署包说明与完整环境变量清单
- [CONTRIBUTING.md](../CONTRIBUTING.md) —— 开发流程、提交规范、提交前敏感信息终检
- [SECURITY.md](../SECURITY.md) —— 漏洞披露
