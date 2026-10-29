---
name: amazon-crawler
description: amazonCrawler 竞品监控工具集——查 ASIN 快照/价格历史/指标报告、管理 watchlist、提交与轮询 Amazon 采集任务；含价格口径与 None 语义警告。
---

# amazon-crawler 使用知识

通过 MCP 服务 amazon-crawler（9 个工具）操作 amazonCrawler 竞品监控系统。
先读本文的选择树选对工具；所有工具 ASIN 必须匹配 `^B0[A-Z0-9]{8}$`（B0 开头 10 位），
非法入参会得到 isError 的结构化 JSON 错误。

## 工具选择树

```
你要做什么？
├─ 看某个商品现在的状态（价格/评分/BSR/变体）
│   └─ get_asin_snapshot(asin)                      # 最新快照
│       └─ 需要历史某天 → get_asin_snapshot(asin, date="YYYY-MM-DD")
├─ 看价格怎么变的
│   └─ get_price_history(asin, days=30)             # 只回答"变了多少"
│       └─ 要更全（BSR/评论/缺货/BuyBox）→ get_metrics_report(asin, days)
├─ 找竞品/爆品
│   ├─ 全部 watchlist 竞品的报告 → list_competitors()
│   │   └─ 按类目过滤 → list_competitors(category="Clothing")
│   └─ 评论增速异常的 → find_surging_products(min_review_velocity=5, window_days=7)
├─ 管理监控名单
│   ├─ 加监控 → add_watchlist(asin, note=..., variant_asin=...)
│   └─ 去掉监控 → remove_watchlist(asin)
└─ 拿最新数据（快照太旧时）
    └─ trigger_scrape(asins=[...], channels=["pdp"])   # 异步，返回 task_id
        └─ 轮询 → get_task_status(task_id)             # 见下方轮询模式
```

先判断"查询"还是"采集"：查询类（snapshot/price/metrics/competitors）读已有数据库，
即时返回；数据不够新才走 trigger_scrape 采集。**不要为了查一次价格就触发采集**——
每次采集都是真实请求 Amazon，触发反爬代价是 1 小时冷却。

## 触发采集后的轮询模式

1. `trigger_scrape(asins=[...])` → 拿 `task_id`。提交成功 ≠ 采集成功。
2. 每 **30–60 秒** 调一次 `get_task_status(task_id)`，直到 `state` 进入
   `completed` / `failed` 终态。`pending` / `running` 期间耐心等，不要连续轰炸查询。
3. 终态后读 `progress`（done/total）和 `errors`（逐 ASIN 失败原因）。
4. `errors` 里出现 `BotBlocked`：已触发反爬、进入 1 小时软冷却。**正确动作是
   降低触发频率、缩小批量**，而不是立刻重试——重试只会延长冷却。
5. 采集完成后重新调查询类工具拿新快照。

## 报告字段解读（口径警告）

### price_scope = 'display_page_price'（重要警告）

所有价格字段的口径是**商品展示页 BuyBox 显示价**，不是 offer 列表最低价、
也不是历史某渠道价。含义：
- 跨商品对比：两边都必须是 display_page_price 口径（本系统保证），可对比；
- 与外部数据（如其他工具抓的 offer 最低价）对比时**口径不同，禁止直接相减**；
- 币种跟随 marketplace（US 站 = USD）。

### variant_status 与 comparison_ready

- `variant_status='fixed'`：解析器已固定变体身份，快照里带 `variant_asin`——
  今天和昨天比的是同一个 SKU，可跨期对比。
- `variant_status='none'`：未固定变体。商品页可能因变体漂移（颜色/尺码切换）
  导致跨期价格/BSR 变化其实是"换了商品"，对比结论不可信。
- `comparison_ready=True`：地区与变体的对比前提满足，可以做横向/纵向对比；
  `False` 时先解决变体固定（add_watchlist 时传 `variant_asin`），再谈趋势分析。

### None = 不可算，不等于 0

任何指标字段是 `None`（JSON `null`）表示**窗口内数据不足、无法计算**：
- `price_change.latest=null`：窗口内缺快照，不是"价格为 0"；
- `review_velocity=null`：窗口内没有两次以上快照，不是"评论增速为 0"；
- 计算平均/求和前必须过滤 null，**禁止把 null 当 0 参与运算**——
  否则"爆品检测"会把没数据的商品当成零增速商品漏掉或误判。

### 其他字段

- `bsr_change`：基于 BSR 首类目 rank 对比（BSR 是 JSON 结构，不是数值差分）；
- `availability_downtime`：窗口内不可售天数；
- `buybox_flips`：BuyBox 卖家易手次数（跳蚤跟卖信号）。

## watchlist 维护流程

1. 加监控：`add_watchlist(asin, note="竞品A-卫衣", variant_asin="B0XXXX...")`。
   **推荐传 variant_asin**（从页面变体下拉确认后填写），否则 variant_status
   停在 'none'，后续对比不可信。
2. 确认入库：调 `list_competitors()` 或直接读
   `src/scheduler/config/watchlist.yaml`，新 ASIN 应出现。
3. 首次数据：新加的 ASIN 数据库里还没有快照，查询全返回 null——先
   `trigger_scrape(asins=[asin])` 采一次基线。
4. 移除：`remove_watchlist(asin)`；对不存在 ASIN 返回 `removed=false`，不是错误。

## 常见错误

| 错误 | 含义 | 正确动作 |
|---|---|---|
| `invalid asin` | ASIN 不匹配 `^B0[A-Z0-9]{8}$` | 修正入参，10 位 B0 开头 |
| `service 'xxx' unavailable` | src/service 层（对应模块）未交付或依赖缺失 | 查 `src/service/` 是否有该模块；不要反复重试 |
| `no snapshot found` | 该 ASIN 从未采集过 | trigger_scrape 采基线 |
| task errors 含 `BotBlocked` | 触发 Amazon 反爬，1h 软冷却 | **降低频率/缩小批量，等待冷却**；不要立刻重试 |
| `unknown task_id` | task_id 写错或进程重启丢了内存任务 | 重新 trigger_scrape |
| 返回里大量 `null` | 窗口天数 > 数据积累天数 | 缩小 days，或先补采历史 |
