# AIYIchat 一键部署脚本（Windows PowerShell）
# 用法：
#   .\deploy.ps1                                     # 默认端口 HTTPS 443 + HTTP 80
#   .\deploy.ps1 -HttpsPort 8443 -HttpPort 8080      # 自定义端口
param(
    [string]$HttpsPort = "",
    [string]$HttpPort = ""
)

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  AIYIchat 一键部署脚本" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# 读取 .env 中指定键的值（不存在时返回 $null）
function Get-EnvPort {
    param([string]$Key)
    if (-not (Test-Path .env)) { return $null }
    $line = Get-Content .env | Where-Object { $_ -match "^$Key=" } | Select-Object -First 1
    if ($line) { return ($line -split "=", 2)[1].Trim() }
    return $null
}

# 在 .env 中写入/更新端口配置（compose 从 .env 读取 HTTPS_PORT / HTTP_PORT）
function Set-EnvPort {
    param([string]$Key, [string]$Value)
    $content = Get-Content .env
    if ($content -match "^$Key=") {
        $content = $content -replace "^$Key=.*", "$Key=$Value"
        Set-Content -Path .env -Value $content
    } else {
        Add-Content -Path .env -Value "$Key=$Value"
    }
}

# 检查 Docker
try {
    docker --version | Out-Null
} catch {
    Write-Host "错误：未安装 Docker，请先安装 Docker Desktop" -ForegroundColor Red
    exit 1
}

# 检查 Docker Compose
try {
    docker compose version | Out-Null
} catch {
    Write-Host "错误：未安装 Docker Compose 2.0+" -ForegroundColor Red
    exit 1
}

Write-Host "[1/5] Docker 环境检查通过" -ForegroundColor Green

# 检查 .env 文件
if (-not (Test-Path .env)) {
    Write-Host "[2/5] 未找到 .env 文件，从 .env.example 复制..." -ForegroundColor Yellow
    Copy-Item .env.example .env
    Write-Host "请编辑 .env 文件配置必要参数（特别是 MONGO_ROOT_PASSWORD 和 DASHSCOPE_API_KEY），然后重新运行本脚本" -ForegroundColor Yellow
    exit 0
} else {
    Write-Host "[2/5] .env 文件已存在" -ForegroundColor Green
}

# 同步端口配置到 .env：命令行参数优先，否则沿用 .env 已有值，再否则使用默认值
if ($HttpsPort -ne "") {
    Set-EnvPort -Key "HTTPS_PORT" -Value $HttpsPort
} else {
    $HttpsPort = Get-EnvPort -Key "HTTPS_PORT"
}
if ($HttpPort -ne "") {
    Set-EnvPort -Key "HTTP_PORT" -Value $HttpPort
} else {
    $HttpPort = Get-EnvPort -Key "HTTP_PORT"
}
if (-not $HttpsPort) { $HttpsPort = "443" }
if (-not $HttpPort) { $HttpPort = "80" }
Set-EnvPort -Key "HTTPS_PORT" -Value $HttpsPort
Set-EnvPort -Key "HTTP_PORT" -Value $HttpPort
Write-Host "服务端口：HTTPS $HttpsPort（主服务）/ HTTP $HttpPort（自动跳转 HTTPS）" -ForegroundColor Green

# 准备 SSL 证书（不入 Git/部署包，首次部署时自动生成自签名证书）
if ((Test-Path certs\selfsigned.crt) -and (Test-Path certs\selfsigned.key)) {
    Write-Host "[3/5] certs/ 目录已存在证书，跳过生成（如需重新生成自签名证书请先删除 certs/ 目录）" -ForegroundColor Green
} else {
    Write-Host "[3/5] 生成自签名证书..." -ForegroundColor Yellow
    New-Item -ItemType Directory -Path certs -Force | Out-Null
    $opensslArgs = @(
        "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-days", "3650",
        "-subj", "/C=CN/O=AIYIchat/CN=localhost",
        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"
    )
    if (Get-Command openssl -ErrorAction SilentlyContinue) {
        # 优先使用本机 openssl
        $opensslArgs += @("-keyout", "certs\selfsigned.key", "-out", "certs\selfsigned.crt")
        & openssl @opensslArgs
    } else {
        # 本机无 openssl 时回退使用 openssl 容器（部署机必装 Docker）
        Write-Host "本机未安装 openssl，使用 alpine/openssl 容器生成..." -ForegroundColor Yellow
        $opensslArgs += @("-keyout", "/certs/selfsigned.key", "-out", "/certs/selfsigned.crt")
        docker run --rm -v "${PWD}/certs:/certs" alpine/openssl @opensslArgs
    }
    if (-not ((Test-Path certs\selfsigned.crt) -and (Test-Path certs\selfsigned.key))) {
        Write-Host "错误：自签名证书生成失败，请检查 openssl 环境或自行准备证书到 certs/ 目录" -ForegroundColor Red
        exit 1
    }
    Write-Host "自签名证书已生成：certs/selfsigned.crt / certs/selfsigned.key（有效期 10 年）" -ForegroundColor Green
}

# 构建并启动
Write-Host "[4/5] 构建并启动容器..." -ForegroundColor Green
docker compose up -d --build

# 等待健康检查
Write-Host "[5/5] 等待服务启动..." -ForegroundColor Green
$maxWait = 60
$waited = 0
$success = $false

while ($waited -lt $maxWait) {
    try {
        $response = Invoke-WebRequest -Uri "https://localhost:${HttpsPort}/api/health" -SkipCertificateCheck -TimeoutSec 2 -ErrorAction SilentlyContinue
        if ($response.StatusCode -eq 200) {
            $success = $true
            break
        }
    } catch {
        # 忽略连接错误，继续等待
    }
    Start-Sleep -Seconds 2
    $waited += 2
    Write-Host "." -NoNewline
}

Write-Host ""

if ($success) {
    Write-Host "========================================" -ForegroundColor Green
    Write-Host "  部署成功！" -ForegroundColor Green
    Write-Host "========================================" -ForegroundColor Green
    Write-Host "  管理后台: https://localhost:${HttpsPort}/admin/login" -ForegroundColor Cyan
    Write-Host "  API 文档:  https://localhost:${HttpsPort}/docs" -ForegroundColor Cyan
    Write-Host "  健康检查: https://localhost:${HttpsPort}/api/health" -ForegroundColor Cyan
    Write-Host "========================================" -ForegroundColor Green
} else {
    Write-Host "警告：健康检查超时，请查看日志确认服务状态：" -ForegroundColor Yellow
    Write-Host "  docker compose logs -f app" -ForegroundColor Yellow
    exit 1
}
