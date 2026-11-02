# 爱易聊 AIYIchat · V1.0 部署包说明

> 一个住在 QQ 和微信里的 AI 聊天伙伴：记得住你说过的话、读得懂你的情绪、会主动来找你，
> 能听语音、看照片、用预置或你复刻的音色回语音、按气氛甩表情包，
> 也可以是 11 位预置角色或你自己造的角色。
>
> 能力与亮点的完整介绍见项目主页 [README](https://github.com/BugWhispererChen/AIYIchat)；本文只讲**怎么装、怎么配、出问题怎么办**。

V1.0 有两种交付形态，按你的使用场景挑一种：

| 形态 | 适合谁 | 你需要准备 | 看哪一节 |
|---|---|---|---|
| **Windows 安装包**（`AIYIchat_Setup_v1.0_20261101.exe`） | 单机自用 / 内网体验，不想碰命令行 | 一个百炼 API Key | [第 1 节](#1-windows-安装包部署) |
| **Linux 部署包 / 源码 + Docker**（`aiyichat-deploy-v1.0-20261101-linux.tar.gz`） | 服务器 / NAS / 对外提供服务 | Docker + 一个百炼 API Key | [第 2 节](#2-linux-部署包部署) |

两条路都需要你自己提供 **阿里云百炼 API Key**（文本、语音、图像共用同一个 Key）。
QQ / 微信 Bot 凭据**不写在配置文件里**——装完之后在管理后台扫码绑定。

---

## 目录

1. [Windows 安装包部署](#1-windows-安装包部署)
2. [Linux 部署包部署](#2-linux-部署包部署)
3. [装完之后要做的三件事](#3-装完之后要做的三件事)
4. [环境变量说明](#4-环境变量说明)
5. [管理后台访问指南](#5-管理后台访问指南)
6. [目录结构](#6-目录结构)
7. [常见问题（FAQ）](#7-常见问题faq)
8. [产物校验与技术支持](#8-产物校验与技术支持)

---

## 1. Windows 安装包部署

### 1.1 系统要求

- Windows 10 / 11（x64；安装包只允许 64 位系统安装）
- 安装时需要**管理员权限**（要注册数据库服务与开机自启项）
- 磁盘：安装包约 110 MB，安装后占用约 340 MB（应用与面板 + 数据库 + 音频工具）；
  建议再预留 1 GB 以上存放聊天记录、生成的图片与日志
- 无需预装 Docker、Python、MongoDB、ffmpeg

### 1.2 安装包自带什么

| 组件 | 说明 |
|---|---|
| **爱易聊服务面板** | 图形面板（`AIYIchatService.exe`），承载 Web 服务，实时显示状态与日志 |
| **MongoDB Community** | 免安装版，安装时注册为 Windows 服务 `AIYIchatMongoDB`，**只监听 `127.0.0.1:27017`**，不对外网开放 |
| **ffmpeg** | 语音转码用，随包附带并自动加入面板进程的查找路径，不需要你另装 |
| **HTTPS 证书** | 首次启动自动生成自签名证书（有效期 10 年），主题备用名包含 `localhost`、`127.0.0.1` 与探测到的局域网 IP |
| **`.env` 配置** | 首次安装写入一份默认配置；覆盖安装时保留你已改过的配置 |

### 1.3 安装步骤

1. 双击 `AIYIchat_Setup_v1.0_20261101.exe`，按向导完成安装。
   勾选「开机自动启动爱易聊服务」可以让它开机自启（写机器级启动项，卸载时自动清理）。
2. 安装结束时勾选「立即启动爱易聊服务面板」（或双击桌面的「爱易聊服务面板」快捷方式）。
   **面板打开后会自动拉起服务，不需要手动点「启动服务」。**
3. 用记事本打开配置文件（默认路径）：

   ```
   C:\Program Files\AIYIchat\AIYIchatService\.env
   ```

   至少填这两项：

   ```ini
   DASHSCOPE_API_KEY=sk-你的百炼Key
   ADMIN_PASSWORD=你自己定的强口令
   ```

4. 回到面板点「停止服务」再点「启动服务」，让新配置生效。
5. 点面板上的「打开管理后台」，或浏览器访问：

   ```
   https://localhost:8443/admin/login
   ```

> **Windows 默认 HTTPS 端口是 8443**（Linux 部署包默认 443）。
> 改端口就改 `.env` 里的 `HTTPS_PORT`，改完重启服务。
> Windows 形态不额外起 HTTP→HTTPS 跳转服务，请用 `https://` 访问。

### 1.4 关于管理密码：留空也行

如果你没填 `ADMIN_PASSWORD`（或填的还是模板里的弱口令），**首次启动会自动生成一个随机强口令，
并只在面板日志里打印这一次**。请当场抄下来——之后重启不会再显示。

之所以这么处理：默认配置模板随源码公开，照抄模板口令等于把后台白送。
（后台对空口令与模板弱口令也是**直接拒绝登录**并提示配置，不会静默放行。）

### 1.5 面板能做什么

- **状态区**：服务状态（未启动 / 启动中 / 运行中 / 停止中 / 异常）、MongoDB 连接、运行时长、访问地址
- **资源区**：进程与系统的 CPU、内存占用
- **运行日志**：滚动显示，同时写入 `AIYIchatService\logs\service.log`（按大小轮转）
- **按钮**：启动服务 / 停止服务 / 打开管理后台 / 退出
- **系统托盘**：点窗口关闭按钮是最小化到托盘继续后台运行，真正退出请用托盘菜单或「退出」按钮；
  面板有单实例互斥保护，开机自启与手动打开不会冲突

### 1.6 卸载与数据

卸载会停止并移除 `AIYIchatMongoDB` 服务，然后询问：

> 是否同时删除数据库数据（聊天记录、角色等业务数据）？

选「否」则数据保留在 `AIYIchatService\mongo_data`，重装后可继续使用。

---

## 2. Linux 部署包部署

### 2.1 系统要求

- Docker 20.10+、Docker Compose 2.0+
- 2 GB 以上可用内存、约 2 GB 可用磁盘
- 开放端口：默认 **443**（HTTPS）+ **80**（HTTP，自动跳转 HTTPS），
  可通过 `.env` 的 `HTTPS_PORT` / `HTTP_PORT` 或部署脚本参数指定

### 2.2 一键部署

```bash
# 1. 解压
tar -xzf aiyichat-deploy-v1.0-20261101-linux.tar.gz
cd aiyichat-deploy-v1.0-20261101-linux

# 2. 配置
cp .env.example .env
# 编辑 .env，至少填 DASHSCOPE_API_KEY 与 ADMIN_PASSWORD（详见第 4 节）

# 3. 部署
chmod +x deploy.sh
./deploy.sh                                        # 默认 HTTPS 443 + HTTP 80
# ./deploy.sh --https-port 8443 --http-port 8080   # 自定义端口
```

部署脚本会：生成自签名证书（若 `certs/` 下没有现成证书）→ 构建镜像 → 起容器 → 等待健康检查。
成功后访问：

- 管理后台 `https://<host>/admin/login`
- 健康检查 `https://<host>/api/health`
- API 文档 `https://<host>/docs`

### 2.3 纯源码形态

Linux 部署包**不做编译混淆**，包内全部为可读 Python 源码：

- 部署机不需要额外构建工具链，`docker compose up -d --build` 即可
- 你可以直接阅读、审计、修改全部业务逻辑
- 打包时反向校验：包内不应出现 `.so` 编译产物，也不应出现 `.env`、`certs/`、`*.db` 等敏感文件

### 2.4 从源码部署（不下载部署包）

```bash
git clone https://github.com/BugWhispererChen/AIYIchat.git && cd AIYIchat/aiyichat
cp .env.example .env      # 同样只填 DASHSCOPE_API_KEY 与 ADMIN_PASSWORD
docker compose up -d --build
docker compose logs --tail=50 app
```

证书同样不需要手工准备：compose 里的 `certgen` 服务会先跑一次
`tools/ensure_certs.py` 生成自签名证书，`app` 服务在它成功退出后才启动。

Windows 上直接从源码跑，可在 `aiyichat/` 目录用 PowerShell 执行根目录的 `deploy.ps1`：

```powershell
.\deploy.ps1                                        # 默认 HTTPS 443 + HTTP 80
# .\deploy.ps1 -HttpsPort 8443 -HttpPort 8080       # 自定义端口
```

---

## 3. 装完之后要做的三件事

1. **确认模型配置**：首次启动会按 `DASHSCOPE_API_KEY` 自动生成 5 组默认配置
   （文本 / 文生图 / 语音生成 / 向量 / 图片修改）。想换模型或换供应商，
   在后台「AI 模型配置」页增删改，**保存即热生效**，不用重启。
2. **绑定渠道**：后台「账号详情 → 渠道管理」——QQ 凭据扫码绑定、微信扫码登录。
   凭据存数据库并支持热加载，无需本地配置文件。
3. **开聊**：11 位预置角色与表情包资源随启动自动入库，把角色绑到 Bot / 用户上就能直接对话。

想开 C 端网页（自助注册 / 流式对话 / 账号中心）：后台「网页账号」页打开开关即可，
入口 `/web/register`、`/web/login`、`/web/chat`、`/web/services`、`/web/account`。
**默认全部关闭**，且关闭时 QQ / 微信链路行为完全不变。

---

## 4. 环境变量说明

> **配置分两处**：`.env` 放基础设施（数据库连接、Key、端口、开关默认值）；
> 业务配置（模型与凭证分组、渠道凭据、功能实验室参数、C 端策略）存数据库、在后台改。
> **同名的键一旦在后台保存过，数据库值会覆盖 `.env`**——改 `.env` 不生效时先去后台看。

| 变量名 | 说明 | 代码默认值 |
|--------|------|-----------|
| **应用配置** |||
| `APP_ENV` | 运行环境 | `development`（模板设为 `production`） |
| `LOG_LEVEL` | 日志级别 | `INFO` |
| **服务端口** |||
| `HTTPS_PORT` | HTTPS 服务端口（宿主机与容器内一致） | `443`（Windows 模板为 `8443`） |
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
| `VOICE_DYNAMIC_ENABLED` | 对方发语音后，窗口内文字消息也以语音回复 | `true` |
| `VOICE_WINDOW_MIN_MINUTES` / `VOICE_WINDOW_MAX_MINUTES` | 上述窗口时长上下限（分钟） | `5` / `30` |
| **对话配置** |||
| `CONTEXT_WINDOW_SIZE` | 上下文窗口大小 | `20` |
| `SUMMARY_TRIGGER_COUNT` | 摘要触发消息数 | `40` |
| **管理后台** |||
| `ADMIN_USERNAME` | 管理员用户名 | `admin` |
| `ADMIN_PASSWORD` | 管理员密码 | 空，**必填**（空或模板弱口令时后台拒绝登录；Windows 面板会自动生成随机强口令） |
| `ADMIN_SESSION_HOURS` | 会话有效期（小时） | `24` |
| `ADMIN_LOGIN_RATE_LIMIT` / `ADMIN_LOGIN_MAX_FAIL` / `ADMIN_LOGIN_LOCKOUT_MINUTES` | 后台防爆破：单 IP 每 10 分钟上限 / 用户名连败上限 / 锁定时长 | `10` / `5` / `15` |
| **主动关怀分享** |||
| `PROACTIVE_SHARE_ENABLED` | 召回触发点按概率改发时事 / 趣事分享 | `true` |
| `PROACTIVE_SHARE_RATIO` | 分享型占比（0~1） | `0.5` |
| `PROACTIVE_SHARE_NEWS_RATIO` | 分享型中联网时事占比（其余为创作趣事） | `0.5` |
| **人际关系网络** |||
| `MEMORY_GRAPH_ENABLED` | Kuzu 图数据库增强（未启用时降级 MongoDB 一跳查询） | `false` |
| `MEMORY_GRAPH_PATH` | Kuzu 数据文件路径（需挂卷持久化） | `./data/entity_graph` |
| **记忆分层与检索护栏** |||
| `MEMORY_STABLE_IN_PREFIX` | 常驻事实留提示词前缀以命中缓存，本轮召回后置 | `true` |
| `MEMORY_DYNAMIC_MAX_CHARS` | 动态层注入字符预算 | `600` |
| `MEMORY_RECALL_TIMEOUT_SECONDS` | 查询向量化超时秒数，超时只注入稳定层 | `3.0` |
| `MEMORY_UPDATE_REQUIRES_VECTOR` | 相似度仅来自字符级降级算法时禁止据此失效旧事实 | `true` |
| **记忆治理与反思层**（默认关闭，可在后台「功能实验室」热调） |||
| `MEMORY_HYGIENE_ENABLED` | 写入时指称归一、点状时效事实标记与过期剔除 | `false` |
| `MEMORY_REFLECTION_ENABLED` | 从多条记忆事实归纳可溯源的高阶洞见 | `false` |
| **C 端网页账号**（默认全部关闭，QQ / 微信链路不受影响） |||
| `WEB_AUTH_ENABLED` | 网页登录总开关；关闭时 `/api/auth/*` 与 `/web/*` 一律 403 | `false` |
| `WEB_REGISTER_ENABLED` | 开放自助注册（需先打开总开关） | `false` |
| `WEB_REGISTER_REQUIRE_REVIEW` | 注册需管理员审核后才可登录 | `true` |
| `WEB_CAPTCHA_ENABLED` | 注册 / 登录算式图形验证码 | `true` |
| `WEB_FREE_QUOTA_TEMPLATE_ID` | 注册赠送额度模板 id（留空 / 不存在回落内置体验额度） | 空 |
| `WEB_MAX_PROFILES` / `WEB_MAX_SERVICES` | 单账号可自助创建的私有角色 / 对话服务上限（`0` = 关闭该自助功能） | `5` / `3` |
| `WEB_SESSION_HOURS` | 会话有效期（小时，滑动过期） | `24` |
| `WEB_SESSION_MAX_CONCURRENT` | 同一身份并发有效会话上限（超出踢最旧） | `3` |
| `WEB_LOGIN_MAX_FAIL` / `WEB_LOGIN_LOCKOUT_MINUTES` | 连续登录失败上限 / 锁定时长（分钟） | `5` / `15` |
| `WEB_RATE_LIMIT_REGISTER` / `WEB_RATE_LIMIT_LOGIN` | 单 IP 每 10 分钟请求上限（`0` = 不限流） | `5` / `20` |
| `TRUST_PROXY_HEADERS` | 是否信任代理写入的 `X-Forwarded-For` | `false` |

> **限流与验证码为单实例内存实现**：容器重启即清空，多实例水平扩展时各实例独立计数会失效，
> 需改为 Redis（`INCR` + `EXPIRE`，key 形如 `rate_limit:register:{ip}`）。
> 默认直连部署按 TCP 对端地址限流。只有反向代理已用 `$remote_addr` **覆写**
> `X-Forwarded-For`（而非追加）时，才可设置 `TRUST_PROXY_HEADERS=true`；
> 否则客户端可自造头绕过限流。
> 这些项同样支持在管理后台「网页账号」页热调；**一旦在后台保存过，数据库值会覆盖 `.env`**。

还有若干对话与记忆的细节旋钮（存在惩罚、摘要字数、话题停滞检测、记忆抽取间隔与上限、
实体归并阈值、主动关怀温度、表情包压缩尺寸等）写在 `.env.example` 里，
每项都有中文注释与默认值，**不改也能跑**，需要调优时按注释改即可。

---

## 5. 管理后台访问指南

1. 访问 `https://localhost/admin/login`（Linux 默认 443 时 URL 不带端口；
   Windows 默认 `https://localhost:8443/admin/login`；自定义端口时替换为对应 HTTPS 端口）
2. 使用 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 登录（Windows 未配置时见 [1.4 节](#14-关于管理密码留空也行)）
3. 后台覆盖：仪表盘、角色管理、账号与渠道、对话记录、记忆管理、多媒体与 AI 模型配置、
   表情包、套餐计费、安全过滤、功能实验室、网页账号、系统指标

---

## 6. 目录结构

Linux 部署包解压后：

```
aiyichat-deploy-v1.0-20261101-linux/
├── app/                    # 业务代码（Python 源码）
│   ├── main.py            # 入口薄壳
│   └── ...                # 各业务模块（每个模块顶部有中文职责说明）
├── data/                   # 随包资源
│   ├── stickers/          # 预置表情包（启动时自动入库）
│   └── preset_portraits/  # 预置角色定妆照
├── tools/                  # 预置角色种子脚本
├── docker-compose.yml      # Docker Compose 配置
├── Dockerfile              # 部署版镜像定义
├── requirements.txt        # 依赖清单
├── .env.example            # 环境变量模板（含逐项中文注释）
├── deploy.sh               # Linux 一键部署脚本
├── http_redirect.py        # HTTP→HTTPS 重定向服务（容器内运行）
└── README.md               # 本文档
```

Windows 安装后（默认 `C:\Program Files\AIYIchat\`）：

```
AIYIchat\
├── AIYIchatService\
│   ├── AIYIchatService.exe # 服务面板（承载 Web 服务）
│   ├── .env                # 你要改的配置
│   ├── certs\              # 自动生成的自签名证书
│   ├── ffmpeg\bin\         # 随包 ffmpeg
│   ├── mongo_data\         # 数据库数据（卸载时可选择保留）
│   ├── logs\               # service.log / mongod.log
│   └── _internal\          # 运行时依赖
├── mongodb\bin\            # MongoDB Community（注册为 Windows 服务）
└── unins000.exe            # 卸载器
```

> `certs/` 不入部署包、不提交 Git，由部署脚本 / 面板 / `docker compose`（一次性 `certgen` 服务）
> 在首次运行时自动生成自签名证书；
> 生产环境可在部署前自行准备正式证书放入 `certs/`（检测到已有证书会跳过生成）。

---

## 7. 常见问题（FAQ）

### Q1. 端口被占用（`port is already allocated`）

通过部署脚本参数（`--https-port` / `--http-port`）或 `.env` 的 `HTTPS_PORT` / `HTTP_PORT`
换一个端口；Windows 上直接改 `.env` 后在面板重启服务。

### Q2. 浏览器提示"证书不受信任"

首次部署自动生成自签名证书，属正常现象，点"高级 → 继续访问"即可。
生产环境建议替换为正式证书（部署前放入 `certs/`，脚本检测到已有证书会跳过自签名生成）。

### Q3. 打不开管理后台

- **先确认协议**：本项目只走 HTTPS，`http://` 会被跳转或拒绝，请用 `https://`
- **确认端口**：Windows 默认 8443、Linux 默认 443，改过 `.env` 就用改后的端口
- **看面板 / 容器日志**：Windows 在面板日志区；Linux 用 `docker compose logs --tail=50 app`

### Q4. 登录后台提示 503 / 说密码没配置

`ADMIN_PASSWORD` 为空或仍是模板弱口令，后台会**直接拒绝登录**并提示配置（防止照抄公开模板把后台白送）。
Windows 面板会在首次启动生成随机强口令并打印在面板日志里一次；
Linux 请自行在 `.env` 设置一个强口令后重启容器。

### Q5. 对方发来消息却没有回复

按顺序检查：① 后台「账号详情 → 渠道管理」里渠道是否在线（Bot 是否已扫码绑定并连接成功）；
② 该用户是否已绑定对话服务（服务决定"扮演谁、记忆与额度归属"）；
③ 额度是否耗尽（文字 / 语音 / 图片分别计量）；④ `DASHSCOPE_API_KEY` 是否有效。

### Q6. 查看日志

```bash
docker compose logs -f app          # 实时
docker compose logs --tail=50 app   # 最近 50 行
```

Windows：面板下方的运行日志区，文件在 `AIYIchatService\logs\service.log`。

### Q7. MongoDB 备份

应用自身不含备份逻辑，需自行调度 `mongodump`。compose 已为 mongo 容器挂载命名卷
`mongo_backups:/backups` 作为备份输出位置，例如：

```bash
docker exec aiyichat-mongo-1 mongodump --archive=/backups/aiyichat.tgz --gzip
docker cp aiyichat-mongo-1:/backups/aiyichat.tgz ./
```

如需改用自己的宿主目录，编辑 `docker-compose.yml` 中 mongo 服务的卷挂载即可。
Windows 上数据目录是 `AIYIchatService\mongo_data`，停服后整目录拷贝即可迁移。

### Q8. 重启与停止

```bash
docker compose restart app   # 重启应用
docker compose down          # 停止服务
```

Windows：面板「停止服务」/「启动服务」；关窗口是收到托盘，不是停止服务。

### Q9. 升级版本

覆盖安装（Windows）或直接换部署包（Linux）即可：
数据库在容器卷 / `mongo_data` 里，不随包覆盖。
**注意**：曾在后台「功能实验室 / 网页账号」改过的配置项存在数据库里，会继续沿用，
不会被新包的 `.env` 覆盖回去。

---

## 8. 产物校验与技术支持

### 8.1 校验你拿到的包

V1.0 两份产物的文件名、体积与 SHA-256 清单维护在项目主页
「[快速开始（V1.0 部署包）](https://github.com/BugWhispererChen/AIYIchat#三快速开始v10-部署包)」一节，
下载页见 [Releases](https://github.com/BugWhispererChen/AIYIchat/releases/tag/v1.0)
（产物不随 Git 仓库分发，只挂在 Release 上）。下载后校验：

```bash
sha256sum aiyichat-deploy-v1.0-20261101-linux.tar.gz          # Linux
```

```powershell
Get-FileHash .\AIYIchat_Setup_v1.0_20261101.exe -Algorithm SHA256   # Windows
```

### 8.2 遇到问题

请在项目 GitHub 仓库提交 Issue（有 Bug 报告模板），附上容器 / 面板日志片段与复现步骤。
**提交前请确认已隐去 `.env` 中的密钥与任何聊天记录内容**——这是公开仓库。

发现安全漏洞请走 Security → Report a vulnerability 私有提交，
见 [SECURITY.md](https://github.com/BugWhispererChen/AIYIchat/blob/developing/SECURITY.md)。

---

**许可**：本项目基于 MIT 许可证开源，详见
[LICENSE](https://github.com/BugWhispererChen/AIYIchat/blob/developing/LICENSE)。
