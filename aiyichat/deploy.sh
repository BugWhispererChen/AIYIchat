#!/bin/bash
# AIYIchat 一键部署脚本（Linux）
# 用法：
#   ./deploy.sh                                  # 默认端口 HTTPS 443 + HTTP 80
#   ./deploy.sh --https-port 8443 --http-port 8080   # 自定义端口
set -e

# 解析端口参数（未指定时沿用 .env 已有值，否则使用默认 443/80）
HTTPS_PORT=""
HTTP_PORT=""
while [ $# -gt 0 ]; do
    case "$1" in
        --https-port)
            HTTPS_PORT="$2"
            shift 2
            ;;
        --http-port)
            HTTP_PORT="$2"
            shift 2
            ;;
        *)
            echo "错误：未知参数 $1"
            echo "用法：$0 [--https-port 端口] [--http-port 端口]"
            exit 1
            ;;
    esac
done

# 在 .env 中写入/更新端口配置（compose 从 .env 读取 HTTPS_PORT / HTTP_PORT）
sync_env_port() {
    local key="$1" value="$2"
    if grep -q "^${key}=" .env 2>/dev/null; then
        sed -i "s/^${key}=.*/${key}=${value}/" .env
    else
        echo "${key}=${value}" >> .env
    fi
}

echo "========================================"
echo "  AIYIchat 一键部署脚本"
echo "========================================"

# 检查 Docker
if ! command -v docker &> /dev/null; then
    echo "错误：未安装 Docker，请先安装 Docker 20.10+"
    exit 1
fi

# 检查 Docker Compose
if ! docker compose version &> /dev/null; then
    echo "错误：未安装 Docker Compose 2.0+"
    exit 1
fi

echo "[1/5] Docker 环境检查通过"

# 确保 Docker 服务开机自启（配合 compose 的 restart: unless-stopped 实现服务开机自动拉起）
# 启用失败仅告警不阻断部署（set -e 下用 if 包裹兼顾非 systemd 环境）
if command -v systemctl &> /dev/null; then
    if ! systemctl is-enabled docker &> /dev/null; then
        if systemctl enable docker &> /dev/null; then
            echo "已开启 Docker 开机自启（systemctl enable docker）"
        else
            echo "警告：开启 Docker 开机自启失败，请手动执行：systemctl enable docker"
        fi
    else
        echo "Docker 开机自启已启用"
    fi
else
    echo "提示：未检测到 systemctl，请自行确认 Docker 已配置开机自启"
fi

# 检查 .env 文件
if [ ! -f .env ]; then
    echo "[2/5] 未找到 .env 文件，从 .env.example 复制..."
    cp .env.example .env
    echo "请编辑 .env 文件配置必要参数（特别是 MONGO_ROOT_PASSWORD 和 DASHSCOPE_API_KEY），然后重新运行本脚本"
    exit 0
else
    echo "[2/5] .env 文件已存在"
fi

# 同步端口配置到 .env：命令行参数优先，否则沿用 .env 已有值，再否则使用默认值
if [ -n "$HTTPS_PORT" ]; then
    sync_env_port HTTPS_PORT "$HTTPS_PORT"
else
    HTTPS_PORT=$(grep "^HTTPS_PORT=" .env 2>/dev/null | cut -d= -f2)
fi
if [ -n "$HTTP_PORT" ]; then
    sync_env_port HTTP_PORT "$HTTP_PORT"
else
    HTTP_PORT=$(grep "^HTTP_PORT=" .env 2>/dev/null | cut -d= -f2)
fi
HTTPS_PORT=${HTTPS_PORT:-443}
HTTP_PORT=${HTTP_PORT:-80}
sync_env_port HTTPS_PORT "$HTTPS_PORT"
sync_env_port HTTP_PORT "$HTTP_PORT"
echo "服务端口：HTTPS ${HTTPS_PORT}（主服务）/ HTTP ${HTTP_PORT}（自动跳转 HTTPS）"

# 准备 SSL 证书（不入 Git/部署包，首次部署时自动生成自签名证书）
if [ -f certs/selfsigned.crt ] && [ -f certs/selfsigned.key ]; then
    echo "[3/5] certs/ 目录已存在证书，跳过生成（如需自签名重新生成请先删除 certs/ 目录）"
else
    echo "[3/5] 生成自签名证书..."
    if ! command -v openssl &> /dev/null; then
        echo "错误：未安装 openssl，无法生成自签名证书，请先安装 openssl 或自行准备证书到 certs/ 目录"
        exit 1
    fi
    mkdir -p certs
    openssl req -x509 -newkey rsa:2048 -nodes \
        -keyout certs/selfsigned.key \
        -out certs/selfsigned.crt \
        -days 3650 \
        -subj "/C=CN/O=AIYIchat/CN=localhost" \
        -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
    echo "自签名证书已生成：certs/selfsigned.crt / certs/selfsigned.key（有效期 10 年）"
fi

# 构建并启动
echo "[4/5] 构建并启动容器..."
docker compose up -d --build

# 等待健康检查
echo "[5/5] 等待服务启动..."
MAX_WAIT=60
WAITED=0
while [ $WAITED -lt $MAX_WAIT ]; do
    HTTP_CODE=$(curl -k -s -o /dev/null -w "%{http_code}" "https://localhost:${HTTPS_PORT}/api/health" 2>/dev/null || echo "000")
    if [ "$HTTP_CODE" = "200" ]; then
        echo ""
        echo "========================================"
        echo "  部署成功！"
        echo "========================================"
        echo "  管理后台: https://localhost:${HTTPS_PORT}/admin/login"
        echo "  API 文档:  https://localhost:${HTTPS_PORT}/docs"
        echo "  健康检查: https://localhost:${HTTPS_PORT}/api/health"
        echo "========================================"
        exit 0
    fi
    sleep 2
    WAITED=$((WAITED + 2))
    echo -n "."
done

echo ""
echo "警告：健康检查超时，请查看日志确认服务状态："
echo "  docker compose logs -f app"
exit 1
