# amazon-crawler MCP Server

amazonCrawler 竞品监控的 MCP 接入层：把 ASIN 快照查询、价格历史、指标报告、
watchlist 管理、采集任务提交/轮询 9 个工具暴露给 Claude Code / Claude Desktop。

- 传输：stdio（`python -m mcp_server`）
- 业务入口：`src/service/*`（Agent D 交付；`watchlist_service.py` 已就绪并实测闭环，
  `scrape_service.py`/`metrics_service.py` 交付前相关工具会返回结构化
  `{"ok": false, "error": "service '...' unavailable: ..."}`，不影响服务启动）
- Python：`/Library/Frameworks/Python.framework/Versions/3.11/bin/python3`（已装 mcp SDK）

## Claude Code 接入

项目根 `.mcp.json`：

```json
{
  "mcpServers": {
    "amazon-crawler": {
      "command": "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3",
      "args": ["-m", "mcp_server"],
      "cwd": "/Users/wuwenbo/Documents/competitor-monitor/amazonCrawler"
    }
  }
}
```

保存后重启 Claude Code 会话即可；`/mcp` 命令可确认 9 个工具已加载。

## Claude Desktop 接入

`claude_desktop_config.json`（macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "amazon-crawler": {
      "command": "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3",
      "args": ["-m", "mcp_server"],
      "cwd": "/Users/wuwenbo/Documents/competitor-monitor/amazonCrawler",
      "env": {}
    }
  }
}
```

重启 Claude Desktop，工具面板出现 amazon-crawler 前缀的 9 个工具即接入成功。

## 工具清单

| 工具 | 签名 | 用途 | 关键返回 |
|---|---|---|---|
| `get_asin_snapshot` | `(asin, marketplace="US", date=None)` | 查最新/指定日期商品快照 | `snapshot`：asin_snapshots 列名字段 |
| `get_price_history` | `(asin, days=30)` | 近 N 天价格变动 | `price_change: {latest, past, change, change_rate}` |
| `get_metrics_report` | `(asin, days=30)` | 综合指标报告 | 价格/BSR/评论增速/缺货/BuyBox 易手 |
| `list_competitors` | `(category=None, sort_by="review_velocity")` | watchlist 竞品报告列表 | `competitors: [report, ...]` |
| `find_surging_products` | `(min_review_velocity=5, window_days=7)` | 评论增速超阈值爆品 | `surging: [...]` |
| `add_watchlist` | `(asin, note="", variant_asin=None)` | 加入 watchlist | `item: {asin, note, variant_asin}` |
| `remove_watchlist` | `(asin)` | 移除 watchlist | `removed: true/false` |
| `trigger_scrape` | `(asins, channels=["pdp"])` | 提交批量采集任务 | `task_id`（异步，需轮询） |
| `get_task_status` | `(task_id)` | 查询采集任务状态 | `status: {state, progress, errors}` |

所有工具：ASIN 校验 `^B0[A-Z0-9]{8}$`（非法 → isError + 结构化 JSON）；返回
`json.dumps(ensure_ascii=False)`；业务异常捕获为 `{"ok": false, "error": ...}`。

## 数据口径（重要）

- `price_scope='display_page_price'`：价格是商品展示页 BuyBox 显示价，**不是**
  offer 列表最低价；跨商品/跨期对比必须同口径。
- `variant_status='fixed'`：解析器已固定变体身份（有 variant_asin）；`'none'`
  表示未固定，跨期对比可能混入变体漂移。
- `comparison_ready=True`：字段口径一致、可横向对比的前提满足。
- 指标字段 `None` = 窗口内数据不足**不可算**，语义不等于 0，禁止当 0 参与计算。

## 故障排查

- `service 'xxx' unavailable`：`src/service/` 对应模块（Agent D）未交付或依赖缺失；
  检查 `src/service/scrape_service.py`、`metrics_service.py` 是否存在。
- 启动即退出：确认用的是 3.11 解释器且已 `pip install mcp`；`mcp 2.x` 兼容已内置。
- `BotBlocked` 出现在 task errors：触发反爬，已进入 1 小时软冷却——降低触发频率，
  不要立刻重试。
- 测试：`python3 -m pytest tests/test_mcp_server.py -q`（21 个用例，全离线）。
