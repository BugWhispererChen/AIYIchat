# AIYIchat 部署包

基于 Prompt Profile 的 QQ/微信多平台角色扮演对话系统。

## 系统要求

- Docker 20.10+
- Docker Compose 2.0+
- 2GB+ 可用内存
- 开放端口：默认 443（HTTPS）+ 80（HTTP，自动跳转 HTTPS），可通过 `.env` 的 `HTTPS_PORT` / `HTTP_PORT` 或部署脚本参数指定

## 快速开始

### 1. 解压部署包

```bash
tar -xzf aiyichat-deploy-YYYYMMDD-linux.tar.gz
cd aiyichat-deploy-YYYYMMDD-linux
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 文件，至少修改以下两项：
# - DASHSCOPE_API_KEY: 阿里云百炼 API Key（文本 + 多媒体共用）
# - ADMIN_PASSWORD: 管理后台登录密码
```

### 3. 一键部署

**Linux:**
```bash
chmod +x deploy.sh
./deploy.sh                                          # 默认 HTTPS 443 + HTTP 80
# ./deploy.sh --https-port 8443 --http-port 8080     # 自定义端口
```

**Windows：** 正式交付形态是单独的 EXE 安装包（由 `tools/windows_service/` 构建，
不含在本 Linux 包内）。若你直接从源码仓库部署，可在 `aiyichat/` 目录用 PowerShell 执行
根目录的 `deploy.ps1`：

```powershell
.\deploy.ps1                                         # 默认 HTTPS 443 + HTTP 80
# .\deploy.ps1 -HttpsPort 8443 -HttpPort 8080        # 自定义端口
```

部署成功后访问（默认端口 443 时 URL 无需带端口，自定义端口时替换为对应 HTTPS 端口）：
- 管理后台: https://localhost/admin/login
- API 文档: https://localhost/docs

## 环境变量说明

| 变量名 | 说明 | 代码默认值 |
|--------|------|-----------|
| **应用配置** |||
| `APP_ENV` | 运行环境 | `development`（模板设为 `production`） |
| `LOG_LEVEL` | 日志级别 | `INFO` |
| **服务端口** |||
| `HTTPS_PORT` | HTTPS 服务端口（宿主机与容器内一致） | `443` |
| `HTTP_PORT` | HTTP 重定向端口（自动跳转 HTTPS） | `80` |
| **MongoDB** |||
| `MONGODB_URI` | MongoDB 连接串 | `mongodb://localhost:27017`（容器编排用 `mongodb://mongo:27017`） |
| `MONGODB_DATABASE` | 数据库名 | `aiyichat` |
| `USE_MOCK_DB` | 是否使用内存 Mock 数据库 | `false` |
| **阿里云百炼** |||
| `DASHSCOPE_API_KEY` | 百炼 API Key（文本 + 多媒体共用） | 空，**必填** |
| **多媒体开关** |||
| `ASR_ENABLED` | 是否启用语音识别 | `false` |
| `VISION_ENABLED` | 是否启用视觉理解 | `false`（模板设为 `true`） |
| `TTS_ENABLED` | 是否启用语音合成 | `false`（模板设为 `true`） |
| `IMAGEGEN_ENABLED` | 是否启用文生图 | `false`（模板设为 `true`） |
| `VOICE_DYNAMIC_ENABLED` | 用户发语音后，窗口内文字消息也以语音回复 | `true` |
| `VOICE_WINDOW_MIN_MINUTES` | 上述窗口时长下限（分钟） | `5` |
| `VOICE_WINDOW_MAX_MINUTES` | 上述窗口时长上限（分钟） | `30` |
| **对话配置** |||
| `CONTEXT_WINDOW_SIZE` | 上下文窗口大小 | `20` |
| `SUMMARY_TRIGGER_COUNT` | 摘要触发消息数 | `40` |
| **管理后台** |||
| `ADMIN_USERNAME` | 管理员用户名 | `admin` |
| `ADMIN_PASSWORD` | 管理员密码 | 空，**必填** |
| `ADMIN_SESSION_HOURS` | 会话有效期（小时） | `24` |
| **主动关怀分享** |||
| `PROACTIVE_SHARE_ENABLED` | 召回触发点按概率改发时事/趣事分享 | `true` |
| `PROACTIVE_SHARE_RATIO` | 分享型占比（0~1） | `0.5` |
| `PROACTIVE_SHARE_NEWS_RATIO` | 分享型中联网时事占比（其余为创作趣事） | `0.5` |
| **人际关系网络** |||
| `MEMORY_GRAPH_ENABLED` | Kuzu 图数据库增强（未启用时降级 MongoDB 一跳查询） | `false` |
| `MEMORY_GRAPH_PATH` | Kuzu 数据文件路径（需挂卷持久化） | `./data/entity_graph` |

> 模型名称、接口地址、API Key 分组等 AI 模型配置**不在 `.env` 里维护**：首次启动时按
> `DASHSCOPE_API_KEY` 自动生成 5 组默认配置（文本/文生图/语音生成/向量/图片修改），
> 之后在管理后台「AI 模型配置」页增删改，保存即热生效。
> QQ / 微信 Bot 凭据同样存于数据库，在后台 Bot 管理页录入。

## 管理后台访问指南

1. 访问 `https://localhost/admin/login`（自定义了 `HTTPS_PORT` 时带上对应端口）
2. 使用 `.env` 中配置的 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 登录
3. 登录后即可使用全部功能（角色、账号、Bot、对话服务、多媒体配置等）

## 目录结构

```
aiyichat-deploy-YYYYMMDD-linux/
├── app/                    # 业务代码（Python 源码）
│   ├── main.py            # 入口薄壳
│   └── ...                # 各业务模块
├── data/                   # 预置资源（表情包、角色定妆照）
├── tools/                  # 预置角色种子脚本
├── docker-compose.yml      # Docker Compose 配置
├── Dockerfile              # 部署版镜像定义
├── .env.example            # 环境变量模板
├── deploy.sh               # Linux 一键部署脚本
├── http_redirect.py        # HTTP→HTTPS 重定向服务（容器内运行）
└── README.md               # 本文档
```

> `certs/` 目录不入部署包、不提交 Git，由部署脚本首次运行时自动生成自签名证书；
> 生产环境可在部署前自行准备正式证书放入 `certs/` 目录（脚本检测到已有证书会跳过生成）。

## 常见问题（FAQ）

### 1. 端口冲突

**问题**：启动时报错 `port is already allocated`

**解决**：通过部署脚本参数（`--https-port` / `--http-port`）或 `.env` 中的 `HTTPS_PORT` / `HTTP_PORT` 指定其他端口，或停止占用对应端口的程序。

### 2. 证书信任

**问题**：浏览器访问时提示"证书不受信任"

**解决**：首次部署时脚本自动生成自签名证书，属于正常现象。点击"高级" → "继续访问"即可。生产环境建议替换为正式证书（部署前将证书放入 `certs/` 目录，脚本检测到已有证书会跳过自签名生成）。

### 3. 查看日志

```bash
# 实时日志
docker compose logs -f app

# 最近 50 行
docker compose logs --tail=50 app
```

### 4. MongoDB 备份

应用自身不含备份逻辑，需自行调度 `mongodump`。compose 已为 mongo 容器挂载命名卷
`mongo_backups:/backups` 作为备份输出位置，例如：

```bash
docker exec aiyichat-mongo-1 mongodump --archive=/backups/aiyichat.tgz --gzip
docker cp aiyichat-mongo-1:/backups/aiyichat.tgz ./
```

如需改用自己的宿主目录，编辑 `docker-compose.yml` 中 mongo 服务的卷挂载即可。

### 5. 重启服务

```bash
docker compose restart app
```

### 6. 停止服务

```bash
docker compose down
```

## 技术支持

如遇到问题，请在项目 GitHub 仓库提交 Issue，附上容器日志片段与复现步骤。
提交前请确认已隐去 `.env` 中的密钥与任何聊天记录内容。

---

**许可**：本项目基于 MIT 许可证开源，详见仓库根目录 `LICENSE`。
