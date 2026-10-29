#!/usr/bin/env bash
# 部署包可用性测试脚本：在隔离环境实际部署并验证能跑起来。
#
# 与 verify_package.sh（静态完整性校验）互补：本脚本做运行级可用性验证，
# 通过独立目录 + 独立 compose 项目名 + 临时端口起一套隔离临时容器，
# 验证部署包解压后能正常构建、启动、通过健康检查，测试后彻底清理，不影响日常环境。
#
# 用法（在服务器上执行）：
#   bash tools/deploy_package/test_deploy_package.sh [部署包路径]
#   不传参数时默认取当前目录下最新的 aiyichat-deploy-*-linux.tar.gz
set -e

# 隔离测试配置：独立目录 / 独立项目名 / 临时端口（避免与日常环境冲突）
TEST_ROOT="${AIYICHAT_TEST_ROOT:-/tmp/aiyichat-deploytest}"
PROJECT="aiyichat-deploytest"
TEST_HTTPS_PORT=9888
TEST_HTTP_PORT=9880
MAX_WAIT=90

# 日常运行目录（复用其 .env 的 MONGODB_URI / DASHSCOPE_API_KEY 等配置）
# 也是 package_deploy.py 的输出目录（包生成在运行目录根部）
RUN_DIR="${AIYICHAT_RUN_DIR:-$(pwd)}"

echo "========================================"
echo "  AIYIchat 部署包可用性测试（隔离环境）"
echo "========================================"

# 1. 定位部署包
PKG="${1:-}"
if [ -z "$PKG" ]; then
    PKG=$(ls -t "$RUN_DIR"/aiyichat-deploy-*-linux.tar.gz 2>/dev/null | head -1)
fi
if [ -z "$PKG" ] || [ ! -f "$PKG" ]; then
    echo "FAIL: 未找到部署包（可传参指定路径）"
    exit 1
fi
echo "测试目标: $PKG"

# 清理函数：无论成败均销毁临时容器/数据卷/测试目录
cleanup() {
    echo ""
    echo "[清理] 销毁临时容器与数据卷..."
    if [ -d "$TEST_ROOT" ]; then
        (cd "$TEST_ROOT" && docker compose -p "$PROJECT" down -v >/dev/null 2>&1) || true
    fi
    rm -rf "$TEST_ROOT"
    echo "[清理] 完成"
}
trap cleanup EXIT

# 2. 解压到独立测试目录
echo ""
echo "[1/6] 解压部署包到隔离目录 $TEST_ROOT ..."
rm -rf "$TEST_ROOT"
mkdir -p "$TEST_ROOT"
tar -xzf "$PKG" -C "$TEST_ROOT"
# 部署包顶层为 aiyichat-deploy-<分支>-<日期>-linux/，进入该目录
PKG_DIR=$(find "$TEST_ROOT" -maxdepth 1 -type d -name 'aiyichat-deploy-*' | head -1)
if [ -z "$PKG_DIR" ]; then
    echo "FAIL: 解压后未找到 aiyichat-deploy-* 顶层目录"
    exit 1
fi
# 将包内容平铺到 TEST_ROOT（compose 需在测试目录根部执行）
# 注意：* 通配符不含 .env.example 等隐藏文件，需开启 dotglob
shopt -s dotglob
mv "$PKG_DIR"/* "$TEST_ROOT"/
shopt -u dotglob
rm -rf "$PKG_DIR"
cd "$TEST_ROOT"
echo "解压完成: $TEST_ROOT"

# 3. 准备测试 .env（复用日常配置，覆盖临时端口）
echo ""
echo "[2/6] 生成测试 .env（临时端口 $TEST_HTTPS_PORT/$TEST_HTTP_PORT）..."
if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
    else
        echo "FAIL: 部署包缺少 .env.example"
        exit 1
    fi
fi
# 复用日常运行目录的关键配置（数据库连接、API Key 等），避免测试环境配置不全
if [ -f "$RUN_DIR/.env" ]; then
    for key in MONGODB_URI MONGODB_DATABASE DASHSCOPE_API_KEY ADMIN_PASSWORD; do
        val=$(grep "^${key}=" "$RUN_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2-)
        if [ -n "$val" ]; then
            if grep -q "^${key}=" .env; then
                sed -i "s|^${key}=.*|${key}=${val}|" .env
            else
                echo "${key}=${val}" >> .env
            fi
        fi
    done
fi
# 强制使用临时端口（覆盖任何继承值）
sed -i "s|^HTTPS_PORT=.*|HTTPS_PORT=${TEST_HTTPS_PORT}|" .env || echo "HTTPS_PORT=${TEST_HTTPS_PORT}" >> .env
grep -q "^HTTPS_PORT=" .env || echo "HTTPS_PORT=${TEST_HTTPS_PORT}" >> .env
sed -i "s|^HTTP_PORT=.*|HTTP_PORT=${TEST_HTTP_PORT}|" .env || echo "HTTP_PORT=${TEST_HTTP_PORT}" >> .env
grep -q "^HTTP_PORT=" .env || echo "HTTP_PORT=${TEST_HTTP_PORT}" >> .env
echo "测试端口: HTTPS=$TEST_HTTPS_PORT HTTP=$TEST_HTTP_PORT"

# 4. 部署脚本自动生成证书（deploy.sh 逻辑）；此处直接构建启动
echo ""
echo "[3/6] 生成自签名证书（模拟 deploy.sh 首次部署）..."
mkdir -p certs
if [ ! -f certs/selfsigned.crt ]; then
    openssl req -x509 -newkey rsa:2048 -nodes \
        -keyout certs/selfsigned.key -out certs/selfsigned.crt \
        -days 365 -subj "/CN=localhost" >/dev/null 2>&1
    echo "证书已生成"
fi

# 5. 用独立项目名构建并启动临时容器
echo ""
echo "[4/6] 构建并启动隔离容器（项目名 $PROJECT）..."
docker compose -p "$PROJECT" up -d --build

# 6. 等待健康检查
echo ""
echo "[5/6] 等待服务启动（健康检查 https://localhost:$TEST_HTTPS_PORT/api/health）..."
WAITED=0
OK=0
while [ $WAITED -lt $MAX_WAIT ]; do
    CODE=$(curl -k -s -o /dev/null -w "%{http_code}" "https://localhost:${TEST_HTTPS_PORT}/api/health" 2>/dev/null || echo "000")
    if [ "$CODE" = "200" ]; then
        OK=1
        break
    fi
    sleep 3
    WAITED=$((WAITED + 3))
    echo -n "."
done
echo ""

if [ "$OK" != "1" ]; then
    echo "FAIL: 健康检查超时（${MAX_WAIT}s 内未返回 200）"
    echo "--- 临时容器日志（最后 30 行）---"
    docker compose -p "$PROJECT" logs --tail=30 app 2>/dev/null || true
    exit 1
fi
echo "健康检查通过（HTTP 200）"

# 7. 验证关键端点
echo ""
echo "[6/6] 验证关键端点..."
ADMIN_CODE=$(curl -k -s -o /dev/null -w "%{http_code}" "https://localhost:${TEST_HTTPS_PORT}/admin/login" 2>/dev/null || echo "000")
echo "  /admin/login -> HTTP $ADMIN_CODE"
if [ "$ADMIN_CODE" != "200" ] && [ "$ADMIN_CODE" != "302" ]; then
    echo "FAIL: 管理后台不可访问（HTTP $ADMIN_CODE）"
    exit 1
fi

echo ""
echo "========================================"
echo "  部署包可用性测试 PASS"
echo "  包: $(basename "$PKG")"
echo "  临时端口 $TEST_HTTPS_PORT 健康检查 + 管理后台均正常"
echo "========================================"
# trap EXIT 会自动清理
exit 0
