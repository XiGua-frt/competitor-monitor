# Amazon 六步采集模块运行报告

报告时间：2026-10-26 16:21:27（北京时间）。

数据来源：保存页面的模拟回放；观察时间和请求计数属于模拟，不是真实新采集。

商品档案 3；历史快照 6；有快照的商品/条件组合 3。

请求账本：过去一小时 5，过去 24 小时 5（含失败、重试及中断前预留）。

运行控制：{"consecutive_errors": 0, "cooldown_until": 1793006487.694956, "next_request_at": 1793002902.694956, "pause_reason": null, "paused": false}

任务状态：{"done": 8, "pending": 1}

## 最新观察

| ASIN | 观察时间（UTC） | 显示价 | 最近有效价格 | 尺码 | 颜色 | 版型 | 相关性 | 变体比较条件 |
|---|---|---|---|---|---|---|---|---|
| B0GWVTMR4Q | 2026-10-26T08:21:12.694956+00:00 | 12.95 USD | 12.95 USD @ 2026-10-26T08:21:12.694956+00:00 | 空 | Light Pink | 空 | related | 未确认 |
| B0H22J6F1B | 2026-10-26T08:20:42.694956+00:00 | 24.99 USD | 24.99 USD @ 2026-10-26T08:20:42.694956+00:00 | 空 | 空 | 空 | related | 未确认 |
| B0DLQ8MVF7 | 2026-10-26T08:20:57.694956+00:00 | 14.99 USD | 14.99 USD @ 2026-10-26T08:20:57.694956+00:00 | Small | Black | Men | related | 未确认 |

## 最近两次观察变化

| ASIN | 价格差 | 页面评分数量差 | 同类目 BSR 变化条数 | 不能比较的原因 |
|---|---|---|---|---|
| B0GWVTMR4Q | 不可计算 | 不可计算 | 0 | variant_unconfirmed_or_changed |
| B0H22J6F1B | 不可计算 | 不可计算 | 0 | variant_unconfirmed_or_changed |
| B0DLQ8MVF7 | 不可计算 | 不可计算 | 0 | variant_unconfirmed_or_changed |

## 最新观察字段空值率

| 字段 | 空值率 |
|---|---|
| price | 0% |
| currency | 0% |
| rating | 0% |
| rating_count | 0% |
| brand | 0% |
| seller_name | 33% |
| date_first_available | 0% |
| best_sellers_ranks | 0% |
| recent_purchase_text | 100% |

报告包含全部最新观察，包括不相关/待确认商品，筛选请查看 relevance.status。
评分数量不等于文字评论数；评分数量差分不等于新增销量；BSR 只在相同类目比较。
近期购买量提示仅保留原文、下界和页面窗口，不相减为新增订单；页面价格不代表最终定制订单金额。
历史、任务、变化与每次运行详细信息见同目录 JSON 文件；持续真实运行稳定性尚未验证。
