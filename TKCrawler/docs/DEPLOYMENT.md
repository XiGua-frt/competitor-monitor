# TKCrawler 部署指南 (DEPLOYMENT)

> 更新时间: 2026-10-07 · 维护: Agent K
> 所有脚本路径、环境变量均已在仓库中核对存在; 未实机验证的步骤已标注 **[待实机验证]**。

## 1. 前置要求

| 依赖 | 版本要求 | 用途 | 验证命令 |
|------|---------|------|---------|
| Python | 3.9+ | 采集/调度/数据库 ORM | `python3 --version` |
| Docker | 20+ | 运行 PostgreSQL/Redis/监控栈 | `docker --version` |
| Docker Compose | v2 (`docker compose`) 或 v1 (`docker-compose`) | 服务编排 | `docker compose version \|\| docker-compose --version` |
| Git | 任意 | 获取代码 | `git --version` |
| TikTok 账号 | 1 个 | 获取登录态 Cookie（单会话，不做账号池） | — |

> 代理: 默认走本地代理出口 (Clash/Surge 等, `PROXY_HOST:PORT`, 默认 `127.0.0.1:7892`)。如 `PROXY_ENABLED=false` 则直连, 但从大陆网络访问 TikTok 通常需要代理。

## 2. 一键安装环境

```bash
cd TKCrawler
bash scripts/setup.sh
```

脚本会: 检查 Python/Docker 版本 → 创建 `.venv` 虚拟环境 → 安装 `requirements.txt` 依赖 → 复制 `.env.example` 为 `.env`（已存在则不覆盖）→ 打印后续步骤。macOS (brew) 与 Linux 均兼容。

## 3. 配置环境变量

编辑 `.env`（变量完整说明见文件内注释及 `src/common/config.py`）:

```bash
TIKTOK_USERNAME=your_username
TIKTOK_PASSWORD=your_password
POSTGRES_PASSWORD=secret_password   # 需与 docker-compose 启动时一致, 见 FAQ 4
```

可选覆盖项（均有代码内置默认值）:

| 变量 | 默认 | 说明 | 读取位置 |
|------|------|------|---------|
| `DATABASE_URL` | 无 | 整串连接符, 优先级高于 `POSTGRES_*` | `src/database/connection.py` |
| `SCRAPER_REQUEST_MIN_INTERVAL` | 7 | 商品页最小请求间隔(秒) | `src/scrapers/base_scraper.py` |
| `SCRAPER_REQUEST_MAX_INTERVAL` | 10 | 商品页最大请求间隔(秒) | `src/scrapers/base_scraper.py` |
| `SCRAPER_SOFT_COOLDOWN_SECONDS` | 3600 | 429/403 后软冷却时长(秒) | `src/scrapers/base_scraper.py` |

## 4. 启动服务 (docker-compose)

⚠️ **重要差异说明**: `docker-compose.yml` 中定义了 `postgres / redis / crawler / prometheus / grafana` 共 5 个服务, 当前仓库状态:

- `crawler` 服务: `Dockerfile` 与 `main.py` 均已落地, 可正常构建（见第 4.1 节）;
- `prometheus` / `grafana` 挂载的 `docs/monitoring/prometheus.yml`、`docs/monitoring/grafana_dashboard.json` **尚未交付**（Agent J 交付物）。

数据层与 crawler 的启动:

```bash
docker-compose up -d postgres redis     # 数据层
docker compose build crawler            # 构建爬虫镜像
```

### 4.1 crawler 服务构建与运行

镜像要点（与 `Dockerfile` 对应）: `python:3.11-slim` 基础镜像、先 COPY `requirements.txt` 装依赖（层缓存友好, 代码变更不重装依赖）、COPY `src/` `scripts/` `main.py`、非 root 用户 `crawler` 运行、`WORKDIR /app`、**不设 ENTRYPOINT**（仅 `CMD ["python", "main.py"]` 作独立运行的兜底默认值）。

ENTRYPOINT/CMD 与 compose command 的语义: compose 的 `command:` 只覆盖镜像的 CMD、**不覆盖 ENTRYPOINT**。compose 中 crawler 的 `command: python main.py scheduler --start` 是完整命令, 若镜像设了 `ENTRYPOINT ["python","main.py"]` 会拼成 `python main.py python main.py ...` 而失败, 因此 Dockerfile 故意不设 ENTRYPOINT。副作用: 用 `docker compose run crawler ...` 传参数时需要带完整的 `python main.py` 前缀, 例如:

```bash
# 容器内 CLI 冒烟验证（--dry-run 不发请求不写库; compose 已内置挂载 src → 代码即改即用）
docker compose run --rm crawler python main.py --dry-run scrape hashtag --tag webdev
```

构建期网络说明与两处依赖补偿（均为 requirements.txt 禁改前提下的构建期处理, 已实证通过）:

1. **构建代理**: 本机直连 Docker Hub / deb.debian.org 会失败时, 用 Docker 预定义的 proxy build-arg 走本地代理构建（基础镜像 `python:3.11-slim` 若无法直拉, 可经 `docker.m.daocloud.io` 镜像源拉取后 `docker tag` 回标准名）:
   ```bash
   docker compose build \
     --build-arg http_proxy=http://host.docker.internal:7892 \
     --build-arg https_proxy=http://host.docker.internal:7892 crawler
   ```
2. **amazon-paapi 过滤**: `requirements.txt` 中的 `amazon-paapi` 在 PyPI 不存在（404, 实际包名为 `python-amazon-paapi` 且代码未 import）, `src/external_data/amazon_client.py` 已自带无第三方依赖实现, Dockerfile 在构建期过滤该行;
3. **psycopg3 补装**: SQLAlchemy 2.x 对 `postgresql://` 默认选择的 DBAPI 已是 psycopg3, `psycopg2-binary` 不再被选中, Dockerfile 额外安装 `psycopg[binary]>=3.1`（与宿主机 venv 行为一致）。

容器凭据注入: 镜像**不含** `.env`（`.dockerignore` 已严格排除, 防止真实凭据进入镜像层）。`TIKTOK_USERNAME/TIKTOK_PASSWORD/POSTGRES_PASSWORD/REDIS_PASSWORD` 等一律由 compose 的 `environment` 段从宿主机 `.env` 插值注入运行中的容器, 镜像本身可安全分发。

已知限制: `camoufox`/`playwright` 的浏览器二进制未在镜像内下载安装, **browser 采集通道在容器内不可用**（API 通道不受影响）; 如需启用 browser 通道, 需在镜像中安装对应系统依赖并执行 `playwright install` / `camoufox fetch`, 以及大量 GTK/X11 apt 包。

监控栈待 `docs/monitoring/` 配置文件就绪后再启动: `docker-compose up -d prometheus grafana`。**[待实机验证]**（Grafana 默认账号 admin / `GRAFANA_PASSWORD`, 默认 admin）

## 5. 初始化数据库

初始化脚本真实路径为 `scripts/init_db.py`, 通过 Alembic 执行 `src/database/migrations/versions/001_initial_schema.py`, 创建 4 张快照表（`product_snapshots` 为按 `snapshot_date` 的 RANGE 分区表）:

```bash
# .venv 激活后
python scripts/init_db.py           # 执行迁移建表
python scripts/init_db.py --check   # 只检查连接与建表结果, 不做变更
```

成功输出示例:

```
========== 建表结果 ==========
[OK]   product_snapshots (17 列): product_id, snapshot_date, title, ...
[OK]   video_snapshots (12 列): video_id, snapshot_date, ...
[OK]   creator_snapshots (8 列): creator_id, snapshot_date, ...
[OK]   hashtag_snapshots (4 列): tag_name, snapshot_date, ...
==============================
数据库初始化完成 ✓
```

> 差异标注: `scripts/init_db.py` 失败时的内置提示写的是 `docker-compose up -d db`, 实际 compose 服务名为 **`postgres`**, 请以 `docker-compose up -d postgres` 为准（脚本提示待上游修正）。

### 5.1 hashtag 日常快照 job (views_growth 差分链路)

`scripts/hashtag_snapshot_job.py` 走实机验证的签名 challenge/detail API 通道（`HashtagTrendScraper.scrape_hashtag_api`, 见 `docs/reverse/hashtag-api.md` E-8）, 逐标签采集 `views_total` → 写入 `hashtag_snapshots` → 打印与上次快照的差分（`views_growth` 增量与增长率; 上期为 0/缺失时增长率按 NULLIF 语义显示 N/A）。写入按 `(tag_name, snapshot_date)` upsert, 同日重跑幂等。

```bash
# .venv 激活后
python scripts/hashtag_snapshot_job.py --dry-run                # 只打印计划, 不请求/不连库
python scripts/hashtag_snapshot_job.py                          # 默认标签: tiktokmademebuyit,webdev
python scripts/hashtag_snapshot_job.py --tags tagA,tagB         # 指定标签 (逗号分隔, 不含 #)
```

- 退出码: `0` 成功（至少 1 个标签写入）/ `1` 采集失败（全部标签失败或触发风控）/ `2` 配置或依赖错误（数据库不可用时提示 `docker-compose up -d postgres`）。
- 标签间随机间隔 8-12s, 模拟真实请求节奏。
- 调度接入: `src/scheduler/config/tasks.yaml` 已定义周期任务 `scrape_hashtag_snapshots`（每日一次, priority 3, payload 带 tags）, `python main.py scheduler --start` 启动后由注册 handler 调用该 job 的 `run_job` 入口。
- 注意: 同一标签当日重复运行只会覆盖当天快照, 差分基准始终为"最近一次早于今日的快照"。

## 6. 验证清单

每步附验证命令, 全部通过即部署完成:

| # | 验证项 | 命令 | 预期 |
|---|--------|------|------|
| 1 | PostgreSQL 健康 | `docker-compose ps` | `tkcrawler_postgres` 状态 `healthy` |
| 2 | Redis 健康 | `docker exec tkcrawler_redis redis-cli ping` | `PONG` |
| 3 | 4 张快照表存在 | `python scripts/init_db.py --check` | 4 个 `[OK]`, 退出码 0 |
| 4 | 表结构核对（可选） | `docker exec -it tkcrawler_postgres psql -U crawler -d tiktok_crawler -c '\dt'` | 列出 4 张表（另含 alembic_version） |
| 5 | 依赖与测试 | `pytest tests/ -v` | 全部通过（当前用例覆盖 database / anti_detection / scheduler / shop_scraper） |
| 6 | 月度分区能力（可选, PG 专属） | `python -c "from src.database.connection import ensure_month_partition; from datetime import date; print(ensure_month_partition(date.today()))"` | `True` |

> 第 5/6 步需在项目根目录且激活 `.venv` 后执行（依赖 `src` 包导入路径）。

## 7. 常见问题 (FAQ)

### 7.1 端口占用

症状: `docker-compose up` 报 `Bind for 0.0.0.0:5432 failed: port is already allocated`。

```bash
lsof -i :5432   # 5432/6379/9090/3000 逐个排查
```

处理: 本机已有 PG/Redis 实例时, 修改 `.env` 中 `POSTGRES_PORT` / `REDIS_PORT` 换端口映射（compose 会读取 `${POSTGRES_PORT:-5432}`）, 再重新 `docker-compose up -d postgres redis`。

### 7.2 PostgreSQL 未启动 / 连接被拒

症状: `python scripts/init_db.py` 报 `could not connect to server` 并提示确认 PG 已启动。

```bash
docker-compose up -d postgres      # 先启动
docker-compose logs postgres       # 再看日志
```

另注意: `.env` 中 `POSTGRES_HOST` 保持 `localhost`（宿主机直跑脚本时）; 若以后在容器网络内运行, 应为 `postgres`（compose 已内置该覆盖）。

### 7.3 Redis 未启动: 任务队列降级为内存队列

`src/scheduler/task_queue.py` 的 `TaskQueue` 在 Redis 不可用时会打 WARNING 并**优雅降级为内存队列（heapq）**:

- 日志特征: `Redis 不可用（...），任务队列降级为内存后备模式，重启后队列数据将丢失`
- 影响: 进程重启后队列内任务**丢失**, 且无跨进程共享; 数据不落盘。
- 建议: 生产/长跑场景务必启动 Redis（`docker-compose up -d redis`）; 短时单进程调试可接受降级。

### 7.4 数据库密码不一致

症状: init_db 报 `password authentication failed`。

- `.env` 的 `POSTGRES_PASSWORD` 需与**首次建库时** compose 传入的值一致（compose 默认 `secret_password`, `src/common/config.py` 内置默认为 `secret`, 以 `.env` 为准即可）。
- 若数据卷已用旧密码初始化: 改 `.env` 回旧密码, 或销毁重建数据卷（**会删数据**, 需人工确认后执行）:

```bash
docker-compose down
docker volume rm tkcrawler_postgres_data   # 卷名以 docker volume ls 为准
docker-compose up -d postgres
```

### 7.5 `docker-compose up -d` 全量启动失败

crawler 服务已可构建（见第 4.1 节）; prometheus/grafana 仍缺 `docs/monitoring/` 配置, 全量启动会在 prometheus 环节失败。现阶段建议只启动 `postgres redis` + 按需 `docker compose run --rm crawler python main.py <子命令>`。

### 7.6 macOS 上 psycopg2 / lxml 安装失败

优先使用 `python3 -m venv .venv` 隔离环境（setup.sh 已做）, 并确保 `brew install python@3.11` 后 PATH 指向 brew 版本。`requirements.txt` 中 psycopg2 为 binary 包, 一般无需本地编译。

---

## 附: 与代码核对发现的差异点（交付时快照）

| 文档/任务卡说法 | 实际代码状态 |
|----------------|-------------|
| README "python main.py scrape ..." | `main.py` 已落地（CLI 子命令 scrape/scheduler/init-db/report） |
| compose `crawler` 服务 | `Dockerfile` 已落地, `docker compose build crawler` 可构建 |
| compose `prometheus/grafana` 挂载 | `docs/monitoring/*` 不存在 |
| init_db.py 提示 `docker-compose up -d db` | 实际服务名为 `postgres` |
| 任务卡商品"15 字段" | `models.py` 实为 15 业务字段 + 主键 `snapshot_date` + `created_at` |
| 任务卡 `gmv_growth_7d` 字段 | 非表字段, 由快照差分计算（`snapshot_engine.calculate_growth`） |
