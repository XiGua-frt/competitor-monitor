#!/usr/bin/env bash
# TKCrawler 一键环境安装脚本 (macOS / Linux)
# 用法: bash scripts/setup.sh
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
fatal() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ---------- 1. 检查 Python 3.9+ ----------
if ! command -v python3 >/dev/null 2>&1; then
    fatal "未找到 python3。macOS 请执行: brew install python@3.11; Linux 请用系统包管理器安装 python3。"
fi
PY_MAJOR_MINOR="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PY_OK="$(python3 -c 'import sys; print(1 if (sys.version_info.major, sys.version_info.minor) >= (3, 9) else 0)')"
if [ "$PY_OK" != "1" ]; then
    fatal "Python 版本过低 ($PY_MAJOR_MINOR), 需要 3.9+。macOS: brew install python@3.11"
fi
info "Python $PY_MAJOR_MINOR 检查通过 ($(command -v python3))"

# ---------- 2. 检查 Docker ----------
if ! command -v docker >/dev/null 2>&1; then
    fatal "未找到 docker。macOS: brew install --cask docker (或安装 Docker Desktop); Linux: 参考 https://docs.docker.com/engine/install/"
fi
if ! docker info >/dev/null 2>&1; then
    warn "docker 已安装但守护进程未运行。macOS 请启动 Docker Desktop; Linux: sudo systemctl start docker"
fi
# 识别 docker compose 子命令或独立 docker-compose
if docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD="docker-compose"
else
    fatal "未找到 Docker Compose。macOS 随 Docker Desktop 附带; Linux: docker-compose-plugin 或独立安装。"
fi
info "Docker 检查通过 (compose 命令: $COMPOSE_CMD)"

# ---------- 3. 创建虚拟环境 ----------
VENV_DIR=".venv"
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
    info "已创建虚拟环境 $VENV_DIR"
else
    info "虚拟环境 $VENV_DIR 已存在, 跳过创建"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
info "已激活虚拟环境 ($(python --version))"

# ---------- 4. 安装依赖 ----------
info "安装依赖: pip install -r requirements.txt (可能需要数分钟)"
pip install --upgrade pip >/dev/null
pip install -r requirements.txt

# ---------- 5. 准备 .env (不覆盖已有) ----------
if [ -f ".env" ]; then
    info ".env 已存在, 不覆盖 (如需重新生成请手动备份后删除)"
else
    cp .env.example .env
    info "已从 .env.example 生成 .env, 请编辑填入 TikTok 账号等配置"
fi

# ---------- 6. 后续步骤提示 ----------
cat <<'NEXT'

========================================
环境安装完成! 后续步骤:
========================================
  1. 编辑 .env 填入 TIKTOK_USERNAME / TIKTOK_PASSWORD 等
  2. 启动数据层服务:
       docker compose up -d postgres redis
     (注: crawler/prometheus/grafana 服务所需文件尚未交付,
      当前阶段不要全量 docker compose up -d, 详见 docs/DEPLOYMENT.md)
  3. 初始化数据库:
       python scripts/init_db.py
       python scripts/init_db.py --check    # 验证建表结果
  4. 运行测试验证环境:
       pytest tests/ -v
  完整说明见 docs/DEPLOYMENT.md, 字段说明见 docs/FIELD_SCHEMA.md
========================================
NEXT
