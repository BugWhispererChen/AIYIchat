FROM python:3.11-slim

ENV LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONIOENCODING=utf-8

WORKDIR /app

# 使用阿里云 Debian 镜像源，安装 ffmpeg + gcc（pilk 编译需要）
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn -r requirements.txt

COPY app/ ./app/
COPY http_redirect.py .
# 预置表情包资源（启动时自动 seed 入库，离线可用）
COPY data/stickers/ ./data/stickers/
# 预置角色种子脚本与定妆照素材（容器内手动执行 seed）
COPY tools/seed_companion_profiles.py ./tools/seed_companion_profiles.py
# 存量数据迁移脚本：README 让在容器内跑 `python -m tools.migrations.*`，
# 不一起 COPY 进镜像的话，Docker 部署下这些迁移命令根本不存在
COPY tools/migrations/ ./tools/migrations/
COPY data/preset_portraits/ ./data/preset_portraits/

# 服务端口可配置（宿主机与容器内一致），默认 HTTPS 443 + HTTP 80
ENV HTTPS_PORT=443 HTTP_PORT=80

EXPOSE 443 80

# HTTPS 主服务（uvicorn）+ HTTP→HTTPS 重定向服务（声音复刻已改 base64 直传，无需对外的 HTTP 媒体下载端口）
CMD ["sh", "-c", "python http_redirect.py & exec python -m uvicorn app.main:app --host 0.0.0.0 --port $HTTPS_PORT --ssl-keyfile /app/certs/selfsigned.key --ssl-certfile /app/certs/selfsigned.crt"]
