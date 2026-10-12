#!/usr/bin/env bash
# 部署包验证脚本：校验部署包完整性、关键文件齐全性与内容规范。
# Windows 产物为单独 EXE 安装包，不适用本脚本。
# 用法（在服务器上，包所在目录执行）：bash verify_package.sh <linux包>
set -e

LINUX_PKG="$1"

check_pkg() {
    local pkg="$1" expected_deploy="$2" forbidden_deploy="$3"
    echo "--- 验证 $pkg ---"
    # 1. tar 完整性
    if ! tar -tzf "$pkg" > /dev/null 2>&1; then
        echo "FAIL: tar 完整性校验失败"; return 1
    fi
    echo "PASS: tar 完整性"

    local file_list
    file_list=$(tar -tzf "$pkg")

    # 2. 平台部署脚本：应包含 expected，不应包含 forbidden
    echo "$file_list" | grep -q "aiyichat-deploy-[^/]*/$expected_deploy$" \
        && echo "PASS: 含 $expected_deploy" || { echo "FAIL: 缺少 $expected_deploy"; return 1; }
    if echo "$file_list" | grep -q "aiyichat-deploy-[^/]*/$forbidden_deploy$"; then
        echo "FAIL: 混入异平台脚本 $forbidden_deploy"; return 1
    fi
    echo "PASS: 无异平台脚本 $forbidden_deploy"

    # 3. 关键文件齐全性
    for f in Dockerfile docker-compose.yml .env.example requirements.txt README.md http_redirect.py; do
        echo "$file_list" | grep -q "aiyichat-deploy-[^/]*/$f$" \
            || { echo "FAIL: 缺少关键文件 $f"; return 1; }
    done
    echo "PASS: 关键配置文件齐全"

    # 4. 源码完整性：部署包为源码形态，关键入口文件必须齐全
    for f in app/main.py app/bootstrap.py app/config.py; do
        echo "$file_list" | grep -q "aiyichat-deploy-[^/]*/$f$" \
            || { echo "FAIL: 缺少源码文件 $f"; return 1; }
    done
    local py_count
    py_count=$(echo "$file_list" | grep -c "app/.*\.py$" || true)
    echo "PASS: app/ 源码 $py_count 个 .py 文件"
    if echo "$file_list" | grep -q "\.so$"; then
        echo "FAIL: 源码形态部署包不应包含 .so 编译产物"; return 1
    fi
    echo "PASS: 无 .so 编译产物（纯源码形态）"

    # 5. 资源与种子脚本
    local sticker_count portrait_count
    sticker_count=$(echo "$file_list" | grep -c "data/stickers/" || true)
    echo "PASS: 表情包资源 $sticker_count 个文件"
    portrait_count=$(echo "$file_list" | grep -c "data/preset_portraits/" || true)
    echo "PASS: 预置定妆照 $portrait_count 个文件"
    echo "$file_list" | grep -q "tools/seed_companion_profiles\.py$" \
        && echo "PASS: 含预置角色种子脚本" \
        || echo "WARN: 缺少 tools/seed_companion_profiles.py"

    # 6. 证书与敏感文件不入包确认
    local sensitive
    sensitive=$(echo "$file_list" \
        | grep -E "certs/|_decrypted/|_ref_src/|private_key|(^|/)\.env$|\.db$" || true)
    if [ -n "$sensitive" ]; then
        echo "FAIL: 包内不应包含证书/敏感数据："
        echo "$sensitive"
        return 1
    fi
    echo "PASS: 证书与敏感文件未入包（证书部署时自动生成）"
}

md5sum "$LINUX_PKG"
check_pkg "$LINUX_PKG" "deploy.sh" "deploy.ps1"
echo ""
echo "=== 全部验证通过 ==="
