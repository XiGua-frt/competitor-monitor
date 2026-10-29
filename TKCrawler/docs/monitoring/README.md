# TKCrawler 监控与可观测性

基于 Prometheus + Grafana 的爬虫监控体系：请求成功率、请求耗时、WAF 拦截、数据空字段率。

## 文件清单

| 文件 | 用途 |
|------|------|
| `prometheus.yml` | Prometheus 抓取配置（由 docker-compose 挂载到 `/etc/prometheus/prometheus.yml`） |
| `alerting_rules.yml` | 告警规则（需挂载到 `/etc/prometheus/alerting_rules.yml`，见下方"已知问题"） |
| `grafana_dashboard.json` | Grafana Dashboard（由 docker-compose 挂载到 `/etc/grafana/provisioning/dashboards/dashboard.json`） |
| `src/monitoring/metrics.py` | 指标定义与便捷函数（采集层接入入口） |

## 指标一览

| 指标 | 类型 | 标签 | 说明 |
|------|------|------|------|
| `scraper_requests_total` | Counter | `scraper`, `status` | 请求数（status: success/failed/waf_blocked 等） |
| `scraper_request_duration_seconds` | Histogram | `scraper` | 请求耗时分布（0.1s~60s 桶） |
| `scraper_waf_blocks_total` | Counter | `scraper` | WAF 拦截次数 |
| `data_quality_missing_fields_ratio` | Gauge | `table` | 表级空字段率（0.0~1.0） |

## 启动监控栈

```bash
# 在项目根目录
docker compose up -d prometheus grafana

# 访问
# Prometheus: http://localhost:9090
# Grafana:    http://localhost:3000 (admin / ${GRAFANA_PASSWORD:-admin})
```

Prometheus 抓取目标（见 prometheus.yml）：

- `crawler:9091` — 爬虫主服务的 metrics 端点（docker 网络内主机名，无需暴露宿主机端口）
- `localhost:9090` — Prometheus 自身

## 采集层接入方法

### 1. 启动 metrics 端点（爬虫进程内）

```python
from src.monitoring import start_metrics_server

start_metrics_server()  # 默认 0.0.0.0:9091，可用环境变量 CRAWLER_METRICS_PORT 覆盖
```

### 2. 记录请求结果

```python
import time
from src.monitoring import observe_request, track_request, inc_waf_block

# 方式 A：手动计时
start = time.monotonic()
try:
    ...执行请求...
    observe_request("shop", "success", time.monotonic() - start)
except WAFBlockedException:
    inc_waf_block("shop")
    observe_request("shop", "waf_blocked", time.monotonic() - start)

# 方式 B：上下文管理器（异常自动记为 failed）
with track_request("video"):
    ...执行请求...
```

### 3. 上报数据质量（写入快照后）

```python
from src.monitoring import set_missing_ratio

ratio = missing_rows / total_rows          # 0.0 ~ 1.0
set_missing_ratio("product_snapshots", ratio)
```

### 指标注册说明

`register_metrics()` 幂等：模块重复导入、函数重复调用均不会抛出 duplicate timeseries 错误。测试中可通过 `register_metrics(registry=自定义Registry)` 隔离。

## 告警规则说明

| 告警 | 触发条件 | 级别 | 含义 |
|------|----------|------|------|
| `ScraperSuccessRateLow` | 成功率 < 80%（5m 窗口，按爬虫分组，持续 5m） | warning | 反检测通道/登录态异常 |
| `WafBlocksHigh` | 5 分钟内 WAF 拦截 ≥ 3 次（立即触发） | critical | 风控收紧，需切换通道或降速 |
| `DataMissingFieldsRatioHigh` | 空字段率 > 30% 持续 5m | warning | 页面结构变化/解析器失效 |

规则由 `prometheus.yml` 的 `rule_files` 引用。通知渠道（Slack/Email）需部署 Alertmanager 并在 prometheus.yml 增加 `alerting:` 段（本轮未含，任务卡中 Alertmanager 为独立组件）。

## 已知问题（待主控协调，本 Agent 无权改 docker-compose.yml）

1. **alerting_rules.yml 未挂载**：docker-compose.yml 的 prometheus 服务目前只挂载了 `prometheus.yml`。需要追加：
   ```yaml
   - ./docs/monitoring/alerting_rules.yml:/etc/prometheus/alerting_rules.yml
   ```
   否则 Prometheus 启动时会因 `rule_files` 指向的文件不存在而报错。
2. **Grafana Dashboard 自动加载不完整**：dashboard JSON 虽已挂载到 provisioning 目录，但缺少 provisioning 配置（`/etc/grafana/provisioning/dashboards/dashboards.yaml` + datasource 配置），Grafana 不会自动加载；需主控在 compose 中补充挂载，或在 UI 中手动导入 `grafana_dashboard.json`。
3. **metrics 端口 9091**：crawler 服务未在 compose 中声明该端口（网络内抓取不需要声明），但需保证爬虫进程实际调用了 `start_metrics_server()`；若修改 `CRAWLER_METRICS_PORT`，需同步修改 prometheus.yml 中的 target。

## 运行测试

```bash
python -m pytest tests/test_monitoring.py -v
```
