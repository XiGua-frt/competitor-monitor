# 账号池监控部署指南（MONITORING_SETUP）

> 更新时间: 2026-10-08
> 对应代码: `src/pools/metrics.py`（`PoolMetricsExporter`）
> 配置文件位置: `docs/monitoring/`

本指南描述如何为**账号池系统**部署 Prometheus + Grafana 监控。

> 注意区分：项目中存在两套指标体系。
> 1. **采集层指标**（`src/monitoring/metrics.py`）：`scraper_requests_total`、`scraper_request_duration_seconds`、`scraper_waf_blocks_total`、`data_quality_missing_fields_ratio`，默认端口 9091（`CRAWLER_METRICS_PORT`）。
> 2. **账号池指标**（`src/pools/metrics.py`）：全部 `pool_` 前缀，默认端口 9090（`POOL_METRICS_PORT`）——本文档主体。
> 两套体系共用 prometheus_client 的全局 REGISTRY，可在同一 `/metrics` 端点共存，但抓取 job 需分别规划（见"注意事项"）。

---

## 1. /metrics 端点与端口

由 `PoolMetricsExporter.start_server()` 提供（内部调用 `prometheus_client.start_http_server`）：

```python
from src.pools import PoolMetricsExporter

exporter = PoolMetricsExporter(cookie_pool=cookie_pool, ip_pool=ip_pool)
exporter.start_server()
# 或模块级便捷函数：
from src.pools.metrics import start_metrics_server
start_metrics_server(port=9090)
```

- **默认绑定**: `0.0.0.0:9090`，路径 `/metrics`
- **端口来源**: 参数 `port` → 环境变量 `POOL_METRICS_PORT` → 默认 `9090`（`src/pools/metrics.py:465` `start_server()`）
- **自动刷新**: 默认 `auto_update=True`，后台线程每 `update_interval`（默认 15s）调用一次 `update_pool_metrics()` 刷新池状态 Gauge；`record_request()` / `record_task_assignment()` 触发 Counter/Histogram 即时更新

## 2. Prometheus 抓取配置

配置文件: `docs/monitoring/prometheus.yml`（docker-compose 挂载为 `/etc/prometheus/prometheus.yml`）要点：

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

rule_files:
  - /etc/prometheus/alerting_rules.yml     # 采集层告警（scraper_* 指标）

scrape_configs:
  - job_name: prometheus
    static_configs:
      - targets: ['localhost:9090']        # Prometheus 自身
  - job_name: crawler
    static_configs:
      - targets: ['crawler:9091']          # 采集层指标（src/monitoring）
```

由 `docker-compose.yml` 的 `prometheus` 服务挂载，端口映射 `9090:9090`，同时挂载 `alerting_rules.yml` 到 `/etc/prometheus/alerting_rules.yml`。

**要让 Prometheus 抓取账号池指标，需在 `scrape_configs` 中补充 job**（配置文件当前没有针对 `pool_` 指标的 job）：

```yaml
  - job_name: pool-metrics
    static_configs:
      - targets: ['crawler:9090']   # 若爬虫进程在同一容器内同时暴露两套指标；
                                    # 若分开部署请改为主机名/端口
```

注意 `9090` 在宿主机侧已被 Prometheus 自身占用（compose 映射），容器网络内部不冲突即可；若账号池 exporter 与 Prometheus 同机部署在宿主机，请用 `POOL_METRICS_PORT` 换一个端口并同步修改 target。

## 3. Grafana 导入 pool_dashboard.json

Dashboard 文件: `docs/monitoring/pool_dashboard.json`（标题 "TKCrawler Pool Management"），面板查询全部使用本文件第 4 节列出的真实指标名。

**方式 A：UI 手动导入（当前 compose 配置下推荐）**

1. 启动监控栈：`docker compose up -d prometheus grafana`
2. 浏览器打开 `http://localhost:3000`（默认账号 `admin` / `${GRAFANA_PASSWORD:-admin}`，compose 中默认 `admin`）
3. 左侧菜单 **Dashboards → New → Import**
4. 点击 **Upload JSON file**，选择 `docs/monitoring/pool_dashboard.json`，或直接粘贴文件内容
5. 数据源选择 Prometheus（首次需在 **Connections → Data sources** 添加，URL 填 `http://prometheus:9090`）
6. 点击 **Import** 完成

**方式 B：Provisioning 自动加载**

compose 已挂载 `docs/monitoring/grafana_dashboards_provider.yml` 到 `/etc/grafana/provisioning/dashboards/dashboards.yml`，但该 provider 只指向 `/etc/grafana/provisioning/dashboards/dashboard.json`（即 `grafana_dashboard.json`，采集层大盘）。`pool_dashboard.json` 未被挂载，需主控在 compose 中追加挂载（如 `./docs/monitoring/pool_dashboard.json:/etc/grafana/provisioning/dashboards/pool.json`），同时确认有 datasource provisioning 配置。

## 4. 暴露的真实指标清单

以下为 `src/pools/metrics.py` 实际定义的全部指标名（启动后 `curl localhost:9090/metrics` 可逐一核对）：

**池状态 Gauge**

| 指标 | 标签 | 说明 |
|------|------|------|
| `pool_accounts_total` | - | 池内账号总数 |
| `pool_accounts_active` | - | 活跃账号数 |
| `pool_accounts_cooling` | - | 冷却中账号数 |
| `pool_accounts_suspended` | - | 挂起账号数 |
| `pool_accounts_failed` | - | 失败账号数 |
| `pool_proxies_total` | - | 代理总数 |
| `pool_proxies_healthy` | - | 健康代理数 |
| `pool_proxies_failed` | - | 失败代理数 |
| `pool_account_health_score` | `status` | 按状态平均健康分（0-1） |
| `pool_account_quota_usage` | - | 平均每日配额使用率（0.0-1.0） |
| `pool_proxy_avg_latency_ms` | `region` | 按区域平均代理延迟（ms） |
| `pool_proxy_success_rate` | `region` | 按区域代理成功率（0.0-1.0） |

**请求 Counter / Histogram**

| 指标 | 标签 | 说明 |
|------|------|------|
| `pool_scraping_requests_total` | `scraper`, `status` | 请求总数（status: success/failed/waf_blocked/rate_limited） |
| `pool_scraping_products_total` | - | 商品采集成功总数（scraper='shop'） |
| `pool_scraping_videos_total` | - | 视频采集成功总数（scraper='video'） |
| `pool_scraping_creators_total` | - | 达人采集成功总数（scraper='creator'） |
| `pool_scraping_waf_blocks_total` | `scraper` | WAF 拦截次数 |
| `pool_scraping_rate_limits_total` | `scraper` | 触发限流次数 |
| `pool_request_duration_seconds` | `scraper` | 请求耗时直方图（桶: 0.1/0.5/1/2/5/8/15/30/60/+Inf，查询时用 `pool_request_duration_seconds_bucket`） |

**任务调度 Counter**

| 指标 | 标签 | 说明 |
|------|------|------|
| `pool_task_assignments_total` | `task_type`, `strategy` | 任务分配成功次数 |
| `pool_task_assignment_failures_total` | `reason` | 任务分配失败次数 |

## 5. 告警规则说明（docs/monitoring/alerts.yml）

`alerts.yml` 是**账号池专用**规则集，指标名与 `src/pools/metrics.py` 完全一致，共 16 条，分 5 组（评估间隔 30s/60s）：

| 告警 | 表达式要点 | 级别 |
|------|-----------|------|
| `PoolActiveAccountsLow` | `pool_accounts_active < 3` 持续 5m | critical |
| `PoolAccountQuotaHigh` | `pool_account_quota_usage > 0.85` 持续 10m | warning |
| `PoolHealthyProxiesLow` | `pool_proxies_healthy < 2` 持续 5m | critical |
| `PoolProxyLatencyHigh` | `pool_proxy_avg_latency_ms > 1000` 持续 5m | warning |
| `PoolAccountHealthScoreLow` | `pool_account_health_score{status="active"} < 0.6` 持续 10m | warning |
| `PoolScrapingSuccessRateLow` | 5m 成功率 < 70% | critical |
| `PoolWafBlocksHigh` | `increase(pool_scraping_waf_blocks_total[5m]) >= 3` | critical |
| `PoolRateLimitsHigh` | `increase(pool_scraping_rate_limits_total[5m]) >= 5` | warning |
| `PoolRequestLatencyHigh` | p95 耗时 > 30s 持续 10m | warning |
| `PoolScraperCompletelyFailed` | 10m 内有请求但零成功 | critical |
| `PoolTaskAssignmentFailureRateHigh` | 分配失败率 > 20% | warning |
| `PoolNoAvailableAccounts` | `reason="no_available_account"` 失败频发 | critical |
| `PoolProductScrapingRateLow` | 商品采集速率 < 0.5/s 持续 15m | warning |
| `PoolVideoScrapingRateLow` | 视频采集速率 < 0.3/s 持续 15m | warning |
| `PoolMetricsDown` / `PoolMetricsScrapeFailed` | `up{job="pool-metrics"} == 0` 或 absent | critical/warning |

`docs/monitoring/alerting_rules.yml` 则是**采集层**规则集（`ScraperSuccessRateLow` / `WafBlocksHigh` / `DataMissingFieldsRatioHigh`），使用 `scraper_*` 与 `data_quality_*` 指标，属于 `src/monitoring/metrics.py` 体系。两套规则互相独立。

通知渠道需自行部署 Alertmanager 并在 prometheus.yml 增加 `alerting:` 段（当前未配置）。

## 6. 验证步骤

```bash
# 1. 启动监控栈
docker compose up -d prometheus grafana

# 2. 在爬虫进程中启动 exporter 后，验证端点
curl -s localhost:9090/metrics | grep '^pool_'

# 应至少看到以下指标名（含 _total/_bucket/_count/_sum 等派生序列）：
# pool_accounts_total / pool_accounts_active / pool_accounts_cooling
# pool_accounts_suspended / pool_accounts_failed
# pool_proxies_total / pool_proxies_healthy / pool_proxies_failed
# pool_account_health_score / pool_account_quota_usage
# pool_proxy_avg_latency_ms / pool_proxy_success_rate
# pool_scraping_requests_total / pool_scraping_products_total
# pool_scraping_videos_total / pool_scraping_creators_total
# pool_scraping_waf_blocks_total / pool_scraping_rate_limits_total
# pool_request_duration_seconds_bucket / _count / _sum
# pool_task_assignments_total / pool_task_assignment_failures_total

# 3. Prometheus targets 页确认抓取状态
# http://localhost:9090/targets   -> 对应 job 应为 UP

# 4. Prometheus 查询冒烟
curl -s 'http://localhost:9090/api/v1/query?query=pool_accounts_active'

# 5. Grafana 面板出图（按第 3 节导入后查看 "TKCrawler Pool Management"）

# 6. 告警规则加载确认
# http://localhost:9090/rules -> 应能看到 alerts.yml 中的 16 条规则
```

## 7. 注意事项（配置与代码的不一致，待主控协调）

以下不一致已核对确认，本文档仅标注，**不修改** `docs/monitoring/` 下配置文件：

1. **Prometheus 抓取目标端口与账号池指标不匹配**：`prometheus.yml` 的 `crawler` job 指向 `crawler:9091`，对应的是采集层指标（`src/monitoring/metrics.py`，`CRAWLER_METRICS_PORT` 默认 9091）。账号池 `PoolMetricsExporter` 默认端口是 **9090**（`POOL_METRICS_PORT`），而 9090 在宿主机已被 Prometheus 容器占用。当前 `prometheus.yml` 中**没有任何 job 抓取 `pool_` 指标**——若要启用本文档的账号池监控，需追加 job（见第 2 节示例）。

2. **`alerts.yml` 未被 `prometheus.yml` 引用**：`rule_files` 只包含 `/etc/prometheus/alerting_rules.yml`（采集层规则）。账号池的 16 条规则（`alerts.yml`）需额外挂载（如 `/etc/prometheus/pool_alerts.yml`）并追加到 `rule_files`，否则不会生效。

3. **`PoolMetricsDown` / `PoolMetricsScrapeFailed` 依赖不存在的 job**：两条规则表达式为 `up{job="pool-metrics"}`，但 `prometheus.yml` 中没有名为 `pool-metrics` 的 job（见第 1 条）。即使加载了 `alerts.yml`，这两条规则在补上对应 job 之前不会按预期触发。

4. **Grafana 未自动加载账号池大盘**：compose 只把 `grafana_dashboard.json` 挂载为 provisioning dashboard（provider 文件指向单一 JSON），`pool_dashboard.json` 未挂载也未在 provider 的 paths 覆盖范围内，需手动导入（第 3 节方式 A）或由主控补充挂载。

5. **命名易混淆提示**：`alerting_rules.yml` 中的 `scraper_requests_total` / `scraper_waf_blocks_total` 与账号池的 `pool_scraping_requests_total` / `pool_scraping_waf_blocks_total` 是**不同指标**，分属两套体系，写告警或面板查询时不要混用。

## 8. 运行测试

```bash
python -m pytest tests/test_monitoring.py -v
```
