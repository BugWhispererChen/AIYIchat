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

# 读取 .env 中某个键的值（不存在返回空串）
get_env_value() {
    grep "^$1=" .env 2>/dev/null | head -n 1 | cut -d= -f2- | tr -d '"'
}

# 拦截随源码公开的默认值：这些口令/占位符一旦放行，后台等于白送给读过 README 的人，
# 模型调用也会全部失败。服务侧对默认口令是 fail-closed 的，此处提前失败更友好。
validate_env() {
    local admin_pw
    admin_pw=$(get_env_value ADMIN_PASSWORD)
    case "$admin_pw" in
        ""|your-admin-password|admin123)
            echo "错误：.env 的 ADMIN_PASSWORD 为空或仍是模板默认值，管理后台将拒绝登录。"
            echo "请设置一个新口令（建议 12 位以上）后重新运行本脚本。"
            echo "可用这条命令生成一个强口令：openssl rand -base64 18"
            exit 1
            ;;
    esac
    if [ "$(get_env_value DASHSCOPE_API_KEY)" = "sk-your-api-key-here" ]; then
        echo "错误：.env 的 DASHSCOPE_API_KEY 仍是占位值，所有模型调用都会失败。"
        echo "请填入阿里云百炼的真实 API Key 后重新运行本脚本"
        exit 1
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
    echo "请编辑 .env 文件配置必要参数（特别是 ADMIN_PASSWORD 后台登录口令、"
    echo "ADMIN_USERNAME 与 DASHSCOPE_API_KEY 模型服务密钥），然后重新运行本脚本"
    exit 0
else
    echo "[2/5] .env 文件已存在"
fi
validate_env

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
