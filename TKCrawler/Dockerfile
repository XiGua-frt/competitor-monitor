# TKCrawler crawler 服务镜像
# 语义约定: 不设 ENTRYPOINT —— docker-compose.yml 中 crawler 服务的
# command `python main.py scheduler --start` 是完整可执行命令, 会覆盖
# 本文件的 CMD; 若设 ENTRYPOINT 会导致 command 被拼接成
# `python main.py python main.py ...` 而失败。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 先只 COPY 依赖清单再安装, 充分利用层缓存:
# requirements.txt 不变时, 代码变更不会触发重装依赖。
# 注意: requirements.txt 为禁改文件, 其中 amazon-paapi 在 PyPI 不存在
# (404, 实际包名为 python-amazon-paapi 且代码未 import, 纯可选),
# src/external_data/amazon_client.py 已自带无依赖实现, 故构建期过滤该行。
COPY requirements.txt .
# indexed-zstd (camoufox → browserforge 的传递依赖) 无 linux/arm64 wheel,
# 源码编译需 gcc/g++/zstd 头文件; 构建工具在同一个 RUN 内安装并清除,
# 不残留在镜像层中。另: SQLAlchemy 2.x 默认 PostgreSQL DBAPI 已是 psycopg3,
# requirements.txt 中的 psycopg2-binary 不再被选中, 故补装 psycopg[binary]
# (与宿主机 venv 行为一致)。
RUN grep -vE '^\s*#|^\s*$|amazon-paapi' requirements.txt > /tmp/requirements.txt \
    && apt-get update \
    && apt-get install -y --no-install-recommends gcc g++ libc6-dev libzstd-dev \
    && pip install --no-cache-dir -r /tmp/requirements.txt "psycopg[binary]>=3.1" \
    && apt-get purge -y gcc g++ libc6-dev libzstd-dev \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# 应用代码 (init-db 子命令依赖 scripts/, 运行时依赖 src/)
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY main.py .

# 运行期目录 (scheduler 状态文件写 /app/data; logs/exports 由 compose 卷挂载覆盖)
RUN mkdir -p /app/data /app/logs /app/exports \
    && useradd --create-home --shell /usr/sbin/nologin crawler \
    && chown -R crawler:crawler /app
USER crawler

# 仅作独立运行镜像的兜底默认值; compose 的 command 会整体覆盖它
CMD ["python", "main.py"]
