# 贡献指南

感谢你对爱易聊（AIYIchat）的关注与贡献！

## 开发环境

```bash
# 1. 创建虚拟环境（Python 3.11）
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/macOS

# 2. 安装依赖
pip install -r aiyichat/requirements.txt

# 3. 准备配置
cp aiyichat/.env.example aiyichat/.env
# 编辑 .env 填入 LLM / 多模态服务的 API Key
```

## 代码规范

- **Type Hints**：所有函数签名必须带类型注解
- **中文注释**：注释与文档字符串使用中文，代码标识符保持英文
- **风格一致**：修改时与周边代码保持一致的风格、命名与注释密度
- **开源工具优先**：代码检查使用 ruff + mypy，测试使用 pytest

## 提交前检查

```bash
cd aiyichat

# 1. 静态检查
.venv/Scripts/python.exe -m ruff check app tools
.venv/Scripts/python.exe -m mypy app

# 2. 运行测试
.venv/Scripts/python.exe tools/test/run_tests.py

# 3. 敏感信息终检（提交前必须执行）
git grep -n -I --cached -E "sk-[A-Za-z0-9]{16,}|LTAI[A-Za-z0-9]{8,}|password.*[:=].*['\"][^'\"]{6,}|\.env$|\.db$|private_key"
```

## Commit Message 规范

- **语言**：中文
- **格式**：`<类型>: <简要描述>`
- **类型关键词**：
  - `功能`：新增功能
  - `修复`：Bug 修复
  - `优化`：性能优化或代码改进
  - `重构`：代码重构（不改变功能）
  - `文档`：文档更新
  - `部署`：部署相关变更
  - `工具`：工具脚本变更

示例：
```
功能: 微信Bot管理界面——扫码登录、状态管理、按钮布局优化
修复: 修复WeChat Bot创建未注册到Manager的问题
工具: 新增sync_to_server.py直接上传部署脚本
```

## PR 流程

1. Fork 本仓库并创建特性分支（`feature/xxx` 或 `fix/xxx`）
2. 提交代码并通过上述检查
3. 提交 PR，填写 PR 模板中的变更说明与测试情况
4. 等待维护者 review，按反馈迭代

## Issue 模板

- **Bug 报告**：请提供复现步骤、环境信息（OS / Python / Docker 版本）、容器日志片段
- **功能建议**：请描述使用场景与预期效果
- **安全漏洞**：**请勿在公开 Issue 提交**，改用 [GitHub 私有漏洞报告](https://github.com/BugWhispererChen/AIYIchat/security/advisories/new)

> **注意**：提交 Issue 或 PR 时，请勿粘贴 `.env` 内容、API Key、聊天记录等敏感信息。
