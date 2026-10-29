# TKCrawler 字段说明 (FIELD_SCHEMA)

> 更新时间: 2026-10-07 · 维护: Agent K
> **字段定义以 `src/database/models.py` 实际代码为准**（4 张快照表均为"主键 + 快照字段 + created_at"结构; 主键均为 `(实体ID, snapshot_date)`, 同一天重复写入走 upsert, 见 `src/database/snapshot_engine.py`）。
> 表: `product_snapshots`（PG 按 snapshot_date RANGE 分区）/ `video_snapshots` / `creator_snapshots` / `hashtag_snapshots`。

---

## 1. 商品快照 `product_snapshots`（15 业务字段）

| 字段 | 类型 | 来源 | 用途 | 示例值 |
|------|------|------|------|--------|
| product_id | String(50), **PK** | 页面采集（商品 URL 提取） | 主键、去重、跨表关联 | `7402xxxxxxxxxxxxx` |
| snapshot_date | Date, **PK** | 系统生成（采集日） | 时序快照维度、增速差分基准 | `2026-10-07` |
| title | String(500), 可空 | 页面采集 | 品类归类、关键词提取、1688 找源 | `Portable Mini Blender USB Rechargeable...` |
| category_l1 | String(100), 可空 | 页面采集（面包屑） | 一级类目筛选、类目均速对比 | `Home & Kitchen` |
| category_l2 | String(100), 可空 | 页面采集（面包屑） | 二级类目筛选 | `Kitchen Appliances` |
| category_l3 | String(100), 可空 | 页面采集（面包屑） | 三级类目精准定位 | `Blenders` |
| price_current | Numeric(10,2), 可空 | 页面采集（当前售价 USD） | 价格带筛选; 派生 GMV 输入 | `19.99` |
| price_original | Numeric(10,2), 可空 | 页面采集（划线原价） | 折扣力度分析 | `29.99` |
| sales_30d | Integer, 可空 | 页面采集 + 派生解析（销量区间取中位数: `"500-1k"`→750, `"10k+"`→10000, 见 `ShopProductScraper.parse_sales_range`） | 需求规模 | `750` |
| gmv_30d_est | Numeric(15,2), 可空 | **派生计算** = `price_current × sales_30d`（`parsers/product_parser.py` 计算, 非页面直接提取） | 市场规模排序 | `14992.50` |
| listed_date | Date, 可空 | 页面采集（上架日期） | 新品/老品判定、打新品池 | `2026-05-12` |
| shop_id | String(50), 可空 | 页面采集 | 店铺维度聚合、店铺重复铺货识别 | `74shopxxxxxxxx` |
| shop_name | String(200), 可空 | 页面采集 | 店铺画像 | `GadgetHut Store` |
| creator_count | Integer, 可空 | 页面采集（"达人带货"模块） | 分发广度; 达人渗透率输入 | `120` |
| video_count_7d | Integer, 可空 | 页面采集 | 内容热度; 内容效率输入 | `85` |
| review_count | Integer, 可空 | 页面采集 | 口碑体量参考 | `1430` |
| rating | Numeric(3,2), 可空 | 页面采集 | 质量信号 | `4.60` |
| created_at | DateTime, 非空, `server_default=now()` | 系统生成 | 入库时间审计 | `2026-10-07 09:00:00` |

> 注: 任务卡称"15 核心字段", 与代码核对后口径为 **15 个业务字段 + 主键 `snapshot_date` + 审计字段 `created_at`**（product_id 为主键不计入业务字段时恰为 15）。

---

## 2. 视频快照 `video_snapshots`（11 字段）

| 字段 | 类型 | 来源 | 用途 | 示例值 |
|------|------|------|------|--------|
| video_id | String(50), **PK** | 页面采集 | 主键、去重 | `7412xxxxxxxxxxxxx` |
| snapshot_date | Date, **PK** | 系统生成 | 差分计算播放/点赞增速 | `2026-10-07` |
| product_id | String(50), 可空 | 页面采集（购物车锚点关联） | 关联商品 | `7402xxxxxxxxxxxxx` |
| author_id | String(50), 可空 | 页面采集 | 关联达人快照 | `6988xxxxxxxxxxxxx` |
| publish_time | DateTime, 可空 | 页面采集 | 视频年龄、发布节奏分析 | `2026-09-20 15:30:00` |
| views | Integer, 可空 | 页面采集 | 曝光规模 | `250000` |
| likes | Integer, 可空 | 页面采集 | 内容吸引力 | `18200` |
| comments | Integer, 可空 | 页面采集 | 互动深度 | `940` |
| shares | Integer, 可空 | 页面采集 | 传播力 | `3100` |
| duration | Integer, 可空 | 页面采集（秒） | 时长-转化关系分析 | `34` |
| is_paid | Boolean, 可空 | 页面采集（Spark Ads 投流标识） | 区分自然流 vs 付费流量 | `false` |
| created_at | DateTime, 非空 | 系统生成 | 审计 | `2026-10-07 09:00:00` |

---

## 3. 达人快照 `creator_snapshots`（7 字段）

| 字段 | 类型 | 来源 | 用途 | 示例值 |
|------|------|------|------|--------|
| creator_id | String(50), **PK** | 页面采集 | 主键、关联视频 author_id | `6988xxxxxxxxxxxxx` |
| snapshot_date | Date, **PK** | 系统生成 | 粉丝/数据增长追踪 | `2026-10-07` |
| nickname | String(200), 可空 | 页面采集 | 展示、搜索 | `@kitchen.hacks` |
| follower_count | Integer, 可空 | 页面采集 | 达人分层（腰部 1-10 万优先） | `58000` |
| avg_views_30d | Integer, 可空 | 派生采集（近 30 个视频取中位数） | 真实带货力评估 | `21000` |
| category_tags | String(500), 可空 | 页面采集（bio + 视频类目, 逗号分隔存储） | 带货类目匹配 | `kitchen,home,gadgets` |
| product_count | Integer, 可空 | 页面采集（橱窗商品数） | 橱窗容量、选品活跃度 | `37` |
| created_at | DateTime, 非空 | 系统生成 | 审计 | `2026-10-07 09:00:00` |

---

## 4. 话题快照 `hashtag_snapshots`（3 字段）

| 字段 | 类型 | 来源 | 用途 | 示例值 |
|------|------|------|------|--------|
| tag_name | String(200), **PK** | 页面采集 | 主键、话题追踪 | `tiktokmademebuyit` |
| snapshot_date | Date, **PK** | 系统生成 | 话题 7 天增速差分 | `2026-10-07` |
| views_total | Integer, 可空 | 页面采集（话题总播放量） | 话题热度趋势 | `12500000000` |
| created_at | DateTime, 非空 | 系统生成 | 审计 | `2026-10-07 09:00:00` |

---

## 5. 派生指标（4 个, 入库后计算, 不落快照表）

> ⚠️ `src/metrics/` 目前为空目录, `MetricsCalculator` 尚未落地（Agent I 交付物）。以下公式来自任务卡（Agent I 卡 + SQL 视图示例）, **未经代码核对实现, 待实现后回填核对**。

| 指标 | 公式 | 用途 |
|------|------|------|
| content_efficiency（内容效率） | `gmv_30d_est ÷ video_count_7d` | 单条内容产出效率, 衡量"一条视频赚多少钱" |
| creator_penetration（达人渗透率） | `creator_count ÷ sales_30d` | 销量中达人贡献占比, 判断是否依赖头部达人 |
| saturation_index（饱和度指数） | `(creator_count × video_count_7d) ÷ gmv_30d_est` | 竞争拥挤度, 越高越卷 |
| surge_coefficient（飙升系数） | `gmv_growth_30d ÷ 类目平均增速` | 识别飙升品（核心指标; `gmv_growth_30d` 由快照差分计算） |

任务卡中的 SQL 视图示例均使用 `NULLIF(denominator, 0)` 防除零——实现时需遵循（无除零错误是 Agent I 验收标准）。

> 与任务卡的差异标注: 任务卡 Agent B 设计稿中出现的 `gmv_growth_7d` / `views_growth_7d` 等增速字段**不是表字段**, 均由 `src/database/snapshot_engine.py` 的差分逻辑（`calculate_growth`）基于两次快照计算, 与 `models.py` 实际建表一致。
