# 部署包生成工具集

本目录包含生成 AIYIchat Linux 部署包的完整工具链。部署包为**纯源码形态**
（不做编译混淆），解压后 `docker compose up -d --build` 即可运行。

## 工具清单

| 文件 | 说明 |
|------|------|
| `package_deploy.py` | 打包主脚本：Git 导出 → 组装 → 打包为 tar.gz |
| `build_package.sh` | 一键打包入口，打包成功后自动触发静态校验与可用性测试 |
| `verify_package.sh` | 部署包静态完整性校验（tar 完整性/关键文件/源码齐全/敏感文件不入包） |
| `test_deploy_package.sh` | 部署包运行级可用性测试（隔离容器 + 临时端口，不影响日常环境） |

> 一键部署脚本 `deploy.sh` 位于 `aiyichat/` 项目根目录，打包时由 `package_deploy.py` 从根目录取并随包交付；
> 本目录曾存在的 `deploy.sh`/`deploy.ps1` 副本从未被引用，已移至 `tools/archive/`。

> Windows 产物形态为单独 EXE 安装包，由本机 `tools/windows_service/` 工具链构建，不走本目录流程。

## 打包工作流

打包有两种代码来源，产出物完全一致：

- **默认（推荐、任何环境可复现）**：不带参数，直接用当前工作目录的代码组装；
- **Git 导出（`--from-git`）**：先 `git archive` 从指定仓库导出再组装，保证「包内容 == 某个提交」，
  避免未提交改动混入产物。仓库路径与分支由环境变量配置：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `AIYICHAT_GIT_BARE_DIR` | 需自行指向本地仓库/裸仓库路径 | Git 仓库路径 |
| `AIYICHAT_GIT_BRANCH` | `developing` | 打包分支 |

流程：

1. **提交修改**：`git add` + `git commit`（commit message 遵循「类型: 简要描述」规范）
2. **打包 + 自动可用性测试**：在 `aiyichat/` 目录执行
   `bash tools/deploy_package/build_package.sh`（脚本内部会自行定位到 `aiyichat/`）
   - 自动完成：取源码 → 组装 → 打包 → 静态校验 → 运行级可用性测试（仅产出 Linux 包）
   - 产物：`aiyichat-deploy-YYYYMMDD-linux.tar.gz`

## 使用方法

### 生成部署包

```bash
# 以下命令均在 aiyichat/ 目录执行（含 app/、docker-compose.yml 的那一层）
python tools/deploy_package/package_deploy.py              # 用当前目录代码打包（默认）
python tools/deploy_package/package_deploy.py --from-git   # 先经 Git 导出再打包（需配环境变量）
```

生成的部署包位于 `aiyichat/` 目录：`aiyichat-deploy-{YYYYMMDD}-linux.tar.gz`。
仅产出 Linux 包（`--platform` 参数保留且仅支持 `linux`）。

### 校验与测试

```bash
# 同样在 aiyichat/ 目录执行
bash tools/deploy_package/verify_package.sh aiyichat-deploy-YYYYMMDD-linux.tar.gz
bash tools/deploy_package/test_deploy_package.sh
```

- `verify_package.sh`：静态完整性校验，秒级完成
- `test_deploy_package.sh`：在独立目录、独立 compose 项目名、临时端口起一套隔离临时容器
  （含独立 MongoDB 数据卷），验证解压后能正常构建、启动、通过健康检查与管理后台访问；
  无论成败均自动 `down -v` 销毁临时容器与数据卷，不影响日常环境

## 部署包内容

- `app/`：业务代码（Python 源码）
- `data/stickers/`：预置表情包资源（启动时自动 seed 入库）
- `data/preset_portraits/`：预置角色定妆照
- `tools/seed_companion_profiles.py`：预置角色种子脚本（启动时自动 seed，也可手动重跑）
- `docker-compose.yml`：Docker Compose 配置
- `Dockerfile`：部署版镜像定义（单阶段）
- `.env.example`：环境变量模板
- `deploy.sh`：Linux 一键部署脚本（首次运行时自动生成自签名证书到 `certs/`，证书不入包不入 Git）
- `README.md`：用户部署文档

## 核心设计

### 1. 代码权威来源为 Git

打包前先从 Git 裸仓库 `git archive` 导出，而非直接使用工作目录，
避免未提交改动混入产物，保证「包内容 == 某个提交」可追溯。

### 2. 源码形态分发

项目为开源项目（MIT），部署包直接分发 Python 源码，不做编译混淆：

- 部署机无需额外构建工具链，`docker compose up -d --build` 即可
- 用户可直接阅读、审计、修改全部业务逻辑
- `verify_package.sh` 反向校验：包内**不应**出现 `.so` 编译产物

### 3. 敏感内容不入包

`verify_package.sh` 强制校验包内不含 `certs/`、`.env`、`*.db`、
`_decrypted/`、`_ref_src/`、`private_key*` 等敏感文件，命中即判定失败。
证书由 `deploy.sh` / `deploy.ps1` 在部署时自动生成自签名版本。

### 4. 双重验证链

静态校验（`verify_package.sh`）只能发现文件缺失类问题，
运行级问题（如 FastAPI 依赖注入被破坏、模板缺失、启动期异常）必须靠
`test_deploy_package.sh` 实际起容器才能暴露。`build_package.sh` 已将两者串联，
打包成功后自动触发可用性测试。

## 验证清单

### 打包产物验证

- [ ] `verify_package.sh` 全部 PASS
- [ ] `app/main.py`、`app/bootstrap.py`、`app/config.py` 齐全
- [ ] 包内无 `.so` 编译产物
- [ ] 包内无 `certs/`、`.env`、`*.db` 等敏感文件
- [ ] 表情包与定妆照资源齐全

### 本地部署验证

- [ ] 解压部署包到临时目录
- [ ] 执行 `docker compose up -d --build` 成功
- [ ] 容器日志确认启动正常（MongoDB、BotManager、调度器）
- [ ] 访问 `https://localhost/api/health` 返回 200（默认 HTTPS 443；自定义端口时替换）
- [ ] 访问 `http://localhost/` 返回 301 跳转到 HTTPS（默认 HTTP 80）
- [ ] 访问 `https://localhost/admin/login` 页面渲染正常
- [ ] 访问 `https://localhost/docs` OpenAPI 文档正常生成

### 服务器部署验证

- [ ] 上传部署包到测试服务器
- [ ] 解压并执行 `deploy.sh`，健康检查自动通过
- [ ] 验证管理后台各页面功能正常
- [ ] 验证对话引擎（含 SSE 流式）、TTS、图片生成等核心功能

## 故障排查

### 打包失败：git archive 报错

**症状**：`错误：git archive 失败`

**原因**：`--from-git` 模式下 `AIYICHAT_GIT_BARE_DIR` 未设置或指向的仓库不存在，
或 `AIYICHAT_GIT_BRANCH` 分支名不匹配

**解决**：核对两个环境变量的实际取值；或不带 `--from-git`，直接用当前目录打包

### 打包失败：源码目录不存在

**症状**：`错误：源码目录不存在: .../app`

**原因**：Git 导出内容缺少 `aiyichat/app/`，或当前目录不是 `aiyichat/`

**解决**：确认在 `aiyichat/` 目录（含 `app/`、`docker-compose.yml` 的那一层）执行脚本

### 可用性测试失败但包已生成

**症状**：`build_package.sh` 打印 `⚠ 警告：部署包可用性测试未通过`

**原因**：运行级问题（依赖注入被破坏、模板缺失、启动期异常等），静态校验无法发现

**解决**：查看测试输出的容器日志定位问题；**修复前不要发布该包**

### 部署失败：磁盘空间不足

**症状**：`apt-get install` 报错 "You don't have enough free space"

**原因**：服务器磁盘已满

**解决**：清理服务器磁盘空间（至少需要 2GB 可用空间）

## 维护说明

- 本工具集与主代码库同步维护
- 修改 `app/` 目录结构或新增随包资源时，需同步更新 `package_deploy.py` 的
  `assemble_package()` 与 `verify_package.sh` 的校验项
- 部署包内的用户文档即仓库中的 `aiyichat/README.md`（打包时交付为包根 `README.md`），
  是唯一副本，改一处即可
