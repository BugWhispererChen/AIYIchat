# 爱易聊（AIYIchat）

> 一个住在 QQ 和微信里的 AI 聊天伙伴——会记住你、懂你的情绪、主动关心你，
> 还能扮演 11 位预置陪伴角色或你自定义的角色陪你聊天。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-compose-blue.svg)](https://docs.docker.com/compose/)

无需下载任何 App，在 QQ 或微信里加个好友就能开始对话。它拥有长期记忆，能听懂你的情绪，
会在你忙碌一天后主动发来一句问候；它可以是预置角色，也可以是你自定义的角色。

---

## 目录

1. [核心功能](#核心功能)
2. [技术架构](#技术架构)
3. [快速部署](#快速部署)
4. [合规与责任](#合规与责任)
5. [素材版权](#素材版权)
6. [路线图](#路线图)
7. [免责声明](#免责声明)
8. [安全披露](#安全披露)

---

## 核心功能

### 1. 双平台即用

通过统一的适配器抽象层（`ChannelAdapter`），一套代码同时接入：

- **QQ**：WebSocket 协议接入，支持多 Bot 并发管理（`BotManager`）
- **微信**：iLink 协议接入，支持扫码登录与多账号管理（`WeChatBotManager`）

用户无需安装额外应用，直接在熟悉的聊天软件里使用。

### 2. 多角色扮演

内置角色（Profile）系统，每个角色由结构化档案（身份信息、语言风格、性格特征、
对话策略）定义，经渲染器生成系统 Prompt：

- 预置角色：**麦筱悠**（在校大学生）+ 5 位女友（**苏晚晴、林小满、顾清欢、
  唐可可、江屿**）+ 5 位男友（**陆知行、秦烈、沈砚、许念安、程叙**），
  人设、语言风格、性格维度差异化设计，各自绑定不同官方音色并配有定妆照
  （种子脚本 `tools/seed_companion_profiles.py`，按 slug 幂等 upsert）
- 支持自定义角色：通过管理后台创建向导（LLM 生成草稿 → 定妆照 → 音色 → 预览确认）
- 角色与 Bot / 用户可灵活绑定，按会话解析当前角色配置

### 3. 角色克隆

- **聊天记录导入**：导入你自己的聊天记录，让 AI 学会你熟悉的聊天风格；数据仅用于生成你自己的角色，可随时删除
- **声音复刻**：上传一段音频即可复刻专属音色，基于阿里云 voice-enrollment + `qwen-audio-3.0-tts-plus`，音频以 base64 直传方式处理，复刻后的音色可直接用于语音回复

> 角色克隆需用户自行准备符合 schema 的 JSON 文件（格式见管理后台「角色克隆」页说明）。
> **仅限导入本人聊天记录**，请遵守所用平台的服务条款与当地法律法规。

### 4. 情绪感知

对话引擎内置情绪检测器（`emotion_detector`），对用户的每一条输入进行情绪分析，
并将情绪信号融入 Prompt 组装，让 AI 的回应贴合当下的心境。

### 5. 长期记忆

上下文管理器（`context_manager`）维护历史消息窗口，并在对话增长时自动触发摘要，
将关键信息沉淀为长期记忆——上周随口提的那件小事，它今天还记得。

### 6. 主动关怀

主动关怀调度器（`proactive/scheduler`）让 AI 不再只是被动等待：
它会结合对话历史与时间感知，在合适的时机主动发起问候，并支持勿扰时间段配置。

### 7. 多模态交互

四路多模态服务，能听、能看、能说、能画：

| 能力 | 实现 | 说明 |
|------|------|------|
| 语音识别（ASR） | 阿里云 Paraformer | 听懂用户发来的语音消息 |
| 语音合成（TTS） | 阿里云 CosyVoice / qwen-audio | 以预置或复刻音色发语音回复 |
| 视觉理解（Vision） | 阿里云 Qwen-VL | 看懂用户发来的图片 |
| 图像生成（ImageGen） | 阿里云 Wanx | 按对话语境为用户画图 |

此外还包含：用户级配额管理与限流、响应安全过滤、管理后台（仪表盘、角色、用户、
对话、Bot、多媒体设置、配额，配置热重载）等配套能力。

---

## 技术架构

### 技术栈

- **Web 框架**：FastAPI（异步）
- **数据库**：MongoDB（Motor 异步驱动，Repository 模式）
- **管理后台**：Jinja2 + HTMX + PicoCSS
- **LLM**：OpenAI 兼容接口（DeepSeek / DashScope），支持多凭证与配置热重载
- **容器化**：Docker + Docker Compose，单机即可运行

### 请求处理流程

```
用户消息
    ↓
QQ WebSocket / 微信 iLink → adapters（平台适配器）
    ↓
NormalizedMessage（统一消息格式）
    ↓
conversation/engine.py（对话引擎）
    ↓
profile_resolver + context_manager + emotion_detector
    ↓
prompt_assembler → llm/openai_provider.py（LLM 调用）
    ↓
response_processor（格式化 / 截断 / 安全过滤）
    ↓
adapters 回发消息（文本 / 语音 / 图片）
```

### 核心模块

| 模块 | 职责 |
|------|------|
| `app/adapters/` | QQ / 微信平台适配器，统一消息接口 |
| `app/conversation/` | 对话引擎：情绪检测、角色解析、上下文管理、响应后处理 |
| `app/llm/` | LLM Provider 抽象与工厂，Prompt 组装 |
| `app/profiles/` | 角色定义、渲染与种子数据 |
| `app/services/` | ASR / TTS / Vision / ImageGen / 配额 / 存储等业务服务 |
| `app/admin/` | Web 管理后台（认证、仪表盘、全功能管理页） |
| `app/db/repositories/` | MongoDB 数据仓库层 |
| `app/proactive/` | 主动关怀调度器 |

详细的目录与文件级结构说明见 [aiyichat/README.md](aiyichat/README.md)。

---

## 快速部署

### 前置要求

- Docker 与 Docker Compose
- 阿里云 DashScope API Key（用于 LLM / ASR / TTS / Vision / ImageGen）

### 部署步骤

```bash
cd aiyichat

# 1. 准备配置
cp .env.example .env
# 编辑 .env：填入数据库连接、LLM 与多模态服务的 API Key、Bot 密钥等

# 2. 准备 Bot 配置（可选）
cp bots.yaml.example bots.yaml

# 3. 构建并启动
docker compose up -d --build

# 4. 查看启动日志
docker compose logs --tail=50 app
```

启动后应用提供：

- **管理后台**：`https://<host>:8848/admin`（登录后可管理角色、Bot、多媒体配置等）
- **健康检查**：`/api/health`

`.env` 关键配置项包括：MongoDB 连接串、LLM 模型与超时（建议 `llm_timeout_seconds=25`）、
Embedding 模型（需显式指定专用向量模型，避免回退到聊天模型导致报错）、
TTS / ASR / Vision / ImageGen 模型选择、管理员账号等。完整模板见 `.env.example`。

---

## 合规与责任

本项目为开源技术项目，**部署方**（即实际运行本项目的个人或组织）需自行承担以下合规义务：

- **生成式 AI 服务备案**：若面向公众提供服务，需按《生成式人工智能服务管理暂行办法》完成算法备案 / 大模型备案
- **内容安全主体责任**：部署方需对 AI 生成内容进行安全过滤与审计（本项目已内置 `safety_checker`，但部署方仍需履行主体责任）
- **未成年人保护**：本项目含情感陪伴类角色，部署方需在显著位置提示未成年人使用限制
- **禁止违法用途**：不得将本项目用于诈骗、骚扰、侵犯他人隐私等违法用途

---

## 素材版权

- **预置表情包**（`data/stickers/`）：来自 [ChineseBQB](https://github.com/zhaoolee/ChineseBQB) 开源表情包数据集，
  素材多为网络流行图，可能存在原始作者版权，商用前请由部署方自行评估；
  如有异议可在管理后台「表情包管理」页逐条停用或删除，并替换为自有素材。详见 [data/stickers/NOTICE.md](aiyichat/data/stickers/NOTICE.md)
- **预置角色定妆照**（`data/preset_portraits/`）：由项目内置 ImageGen 服务（阿里云 Wanx）依
  `tools/seed_companion_profiles.py` 中的 prompt 生成，非真人肖像、非第三方素材。详见 [data/preset_portraits/NOTICE.md](aiyichat/data/preset_portraits/NOTICE.md)

---

## 路线图

### 已完成

- QQ WebSocket + 微信 iLink 双平台接入与多 Bot 管理
- 角色系统（预置角色 + 自定义角色 + 聊天记录风格克隆）
- 长期记忆与自动摘要
- 情绪检测与情绪感知回复
- 主动关怀调度（含勿扰时间）
- 四路多模态（ASR / TTS / Vision / ImageGen）与声音复刻
- 管理后台全功能 + 配置热重载
- 用户配额体系与限流
- Docker Compose 一键部署

### 规划中

- 更多预置角色与角色市场式管理
- 记忆系统的语义检索增强
- 多轮对话中的更自然的多模态穿插交互
- 更完善的可观测性（对话质量、服务调用指标看板）

---

## 免责声明

本项目所有对话内容由人工智能生成，仅供娱乐与陪伴用途，不构成任何医疗、心理咨询建议。
AI 伙伴不能替代真人交流与专业心理服务；如您正处于情绪困境，请联系专业心理援助机构
（全国心理援助热线 12356）。未成年人请在监护人指导下使用。

角色克隆功能仅限使用本人聊天记录，相关数据可随时删除。

---

## 安全披露

如发现安全漏洞，请通过 GitHub 私有漏洞报告（Security → Report a vulnerability）提交，
请勿在公开 Issue 中披露。详见 [SECURITY.md](SECURITY.md)。

---

## 许可证

本项目基于 [MIT License](LICENSE) 开源。
