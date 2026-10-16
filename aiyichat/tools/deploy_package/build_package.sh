#!/usr/bin/env bash
# 一键打包脚本：取最新代码生成部署包，并自动执行校验与可用性测试。
# 仅产出 Linux 包（aiyichat-deploy-<日期>-linux.tar.gz）；
# Windows 产物形态为单独 EXE 安装包，由本地 tools/windows_service/ 工具链构建。
#
# 用法（任意目录均可，脚本自动切到 aiyichat/）：
#     bash tools/deploy_package/build_package.sh
set -e

cd "$(dirname "$0")/../.."

echo "=== 导出最新代码并打包 ==="
# 优先 python3（服务器通常无 python 命令）；python 作为兼容回退
PYTHON=$(command -v python3 || command -v python)
"$PYTHON" tools/deploy_package/package_deploy.py --from-git

echo ""
echo "=== 打包完成 ==="
ls -lh aiyichat-deploy-*.tar.gz | tail -3

# 打包成功后自动做运行级可用性测试（独立目录+临时端口隔离，不影响日常环境）。
# 测试失败不阻断包生成，但明确告警提示需排查部署包可用性。
echo ""
echo "=== 部署包可用性测试（隔离环境自动验证）==="
if bash tools/deploy_package/test_deploy_package.sh; then
    echo "✓ 部署包可用性测试通过"
else
    echo "⚠ 警告：部署包可用性测试未通过，请排查后再发布" >&2
fi
