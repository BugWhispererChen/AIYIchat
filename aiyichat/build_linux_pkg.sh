#!/bin/bash
# Linux 部署包构建脚本（服务器执行）
set -e
cd "$(dirname "$0")"

echo "=== 开始构建 Linux 部署包 ==="
echo "时间: $(date)"

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo "错误: python3 未安装"
    exit 1
fi
echo "Python: $(python3 --version)"

# 检查 Docker
if ! command -v docker &> /dev/null; then
    echo "错误: docker 未安装"
    exit 1
fi
echo "Docker: $(docker --version)"

# 执行构建
echo ""
echo "执行 package_deploy.py --local ..."
python3 tools/deploy_package/package_deploy.py --local

echo ""
echo "=== 构建完成 ==="
ls -lh aiyichat-deploy-*.tar.gz 2>/dev/null || echo "未找到部署包"
