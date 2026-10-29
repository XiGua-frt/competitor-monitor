# Amazon 商品记录数据格式（amazon-product-record-v1）

> 生成时间：2026-10-12 · 用途：统一数据库交接 · 外部验证层（Amazon 交叉验证通道）
> 格式基准：实际采集样例 `B0DLQ8MVF7`（pet photo T-Shirt，2026-10-08 采集）
> 机读 Schema：`docs/schemas/amazon-product-record-v1.schema.json`（JSON Schema draft-07，**已用真实样例验证通过**）

---

## 1. 定位与来源

| 项 | 说明 |
|----|------|
| 这是什么 | Amazon 美区前台商品采集的**单条记录格式**（一条 JSON = 一个 ASIN 的一次采集快照） |
| 谁产出 | Windows 侧 amzpy 前台爬虫（样例 `evidence.import_source` 指向 `crawler-lab/amzpy` 导出的 HTML） |
| 与 TKCrawler 的关系 | **外部验证层**数据源之一（对应规划中 "Amazon 前台 BSR/评论/价格 → 需求交叉验证 + 定价锚点"）。注意：本地 `src/external_data/amazon_client.py` 是 PA-API v5 的 BSR 查询客户端，与本格式**无关**，两者并存 |
| 设计目标 | **跨平台统一记录**：`platform` 字段区分平台，`stats`/`author_id`/`hashtags`/`published_at` 是 TikTok 等社交平台的兼容槽位，Amazon 场景下恒为 null/空 |
| 主键 | `external_id`（ASIN，10 位，`B0` 开头）+ `fetched_at`（快照时间），天然构成时序快照 |

**顶层结构总览**（12 个一级键）：

```
platform, external_id, source_url, title, description, cover_url,
media_urls, fetched_at, published_at, author_id, hashtags,
target_status_code, stats, commerce, collection_conditions,
evidence, relevance, search, storage
```

---

## 2. 字段详单

### 2.1 标识与内容（记录骨架）

| 字段 | 类型 | 必填 | 说明 | 样例值 |
|------|------|------|------|--------|
| platform | string | ✅ | 平台标识，Amazon 恒为 `"amazon"` | `amazon` |
| external_id | string | ✅ | **ASIN**，业务主键。10 位，`B0` 前缀 | `B0DLQ8MVF7` |
| source_url | string | ✅ | 商品详情页 URL | `https://www.amazon.com/dp/B0DLQ8MVF7` |
| title | string | ✅ | 商品标题 | `Custom Best Friend Dog, Cat, or Pet Photo T-Shirt...` |
| description | string/null | — | 卖点文案（feature bullets 以 `\n` 连接） | `Customize by uploading a photo...` |
| cover_url | string/null | — | 主图 URL | `https://m.media-amazon.com/images/I/...` |
| media_urls | string[] | — | 全部图片 URL 列表 | `[1 个 URL]` |
| fetched_at | string | ✅ | 采集时间，ISO8601 UTC 带微秒 | `2026-10-08T10:20:04.796484+00:00` |
| published_at | null | — | 跨平台兼容槽位（社交平台用），Amazon 恒 null | `null` |
| author_id | null | — | 跨平台兼容槽位（达人 ID），Amazon 恒 null | `null` |
| hashtags | [] | — | 跨平台兼容槽位（话题），Amazon 恒空数组 | `[]` |
| target_status_code | string | ✅ | 采集完整度：`full` / `partial` / `failed` | `partial` |

### 2.2 `commerce` — 交易与口碑数据（选品分析核心）

| 字段 | 类型 | 说明 | 样例值 |
|------|------|------|--------|
| price | number | **展示价**（USD）。⚠️ 是页面标价，非定制订单最终价，口径见 `price_scope` | `14.99` |
| currency | string | 币种，固定 `USD` | `USD` |
| availability_text | string/null | 库存状态原文 | `In Stock` |
| brand | string/null | 品牌 | `Modify by Amazon Merch on Demand` |
| seller_name | string/null | 卖家名 | `null` |
| rating | number/null | 评分（0-5） | `4.6` |
| rating_count | integer/null | 评分人数（global ratings） | `104` |
| written_review_count | integer/null | 文字评论数 | `null` |
| date_first_available | string/null | 上架日期，**标准化 ISO 格式** `YYYY-MM-DD` | `2024-11-01` |
| date_first_available_raw | string/null | 上架日期页面原文，审计用 | `November 1, 2024` |
| best_sellers_ranks | array/null | **结构化 BSR**：`[{category, rank}]`，一个商品可挂多个类目排名 | `[{Clothing...: 729415}, {Boys' Novelty...: 20009}, ...]` |
| best_sellers_rank_raw | string/null | BSR 页面原文，审计用，**勿解析**（以结构化字段为准） | `"#729,415 in Clothing..."` |
| recent_purchase_text / _display | string/null | 近期购买提示 | `null` |

### 2.3 `collection_conditions` — 采集条件与变体绑定状态

> 这是本格式最有价值的部分之一：**显式声明"这次采集在什么条件下完成、变体绑定是否已验证"**，防止不同地区/语言/变体的数据被当作可比数据混用。

| 字段 | 类型 | 说明 | 样例值 |
|------|------|------|--------|
| status | string | `confirmed` / `unconfirmed` — 采集条件整体是否达标 | `unconfirmed` |
| comparison_ready | boolean | 数据是否可用于跨商品横向比较 | `false` |
| region_verified | boolean | 地区条件（邮编/语言/币种）是否验证通过 | `true` |
| requested_asin | string | 请求的 ASIN | `B0DLQ8MVF7` |
| variant_status | string | 变体绑定状态：`fixed` / `not_fixed` | `not_fixed` |
| expected_variant | string/null | 期望变体（若指定） | `null` |
| reasons | string[] | 未达标原因码 | `["variant_binding_unverified"]` |
| **expected** | object | 采集前的条件声明（合同）：marketplace / currency / language / delivery_postcode / **price_scope** / **variant_policy** | 见样例 |
| **observed** | object | 实际观测：页面 URL、语言、币种、配送地、**选中的变体维度**（color/size/fit_type）及 `size_option_asin`（变体真实 ASIN） | `Black / Small / Men / B0752XJYNL` |
| observed.variant_evidence | object | 每个变体维度的取证：value + source + **binding（verified/unverified）** + associated_identity | 见样例 |

**两个固定口径常量**（`expected` 中声明，消费方必须遵守）：
- `price_scope: "displayed_product_price_not_final_customized_order"` — price 是展示价，不是定制后的下单价
- `variant_policy: "record_selected_then_fix_per_product"` — 先记录当前选中变体，后续再按商品固定

### 2.4 `evidence` — 字段级溯源

> 每个关键值"从页面哪个 DOM 位置提取的"取证，供审计与解析器回归调试。

| 字段 | 说明 | 样例值 |
|------|------|--------|
| price_raw | 价格原文 | `$14.99` |
| price_source | 价格的 DOM 来源 | `main_product_price_container` |
| price_missing_reason | 缺价原因（有值时解释为什么没抓到） | `null` |
| price_scope | 同 commerce 口径 | `displayed_product_price...` |
| rating_count_raw | 评分原文 | `104 global ratings` |
| description_source | 文案来源 | `feature_bullets` |
| media_urls_complete | 图片是否抓全 | `false` |
| us_preferences_verified | 美区偏好是否验证 | `true` |
| currency_preference / delivery_location | 采集时浏览器偏好 | `USD` / `New York 10001` |
| import_source | 本地证据 HTML 路径 | `F:\\shop\\crawler-lab\\...` ⚠️ **交接外发时脱敏/清空** |

### 2.5 `relevance` — 选品相关性判定（规则引擎输出）

> 判断该商品是否命中垂类（本例：人穿的宠物肖像定制服饰）。带 `rule_version`，规则升级后可对历史记录重跑复现。

| 字段 | 说明 | 样例值 |
|------|------|--------|
| status | `related` / `unrelated` / `unknown` | `related` |
| product_scope | `pet_specific` / `generic` | `pet_specific` |
| target | 判定目标描述 | `human-worn custom pet portrait apparel` |
| rule_version | **规则版本号**，可复现的关键 | `pet-apparel-v1` |
| method | 判定方法说明 | `title_and_product_description_rules_not_image_or_order_validation` |
| stage | 判定阶段 | `detail` |
| reasons | 命中原因码 | `["apparel_customization_and_pet_portrait_evidence"]` |
| evidence[] | 逐条证据：`signal`（apparel/customization/pet_portrait）+ `matched_text` + `excerpt` + `source`（title/description） | 6 条证据 |

### 2.6 `search` — 发现上下文

| 字段 | 说明 | 样例值 |
|------|------|--------|
| keyword | 命中关键词 | `custom pet portrait shirt` |
| selected_result_position | 搜索结果位次 | `1` |
| position_scope | 位次口径声明 | `order_returned_by_amzpy_not_verified_page_rank`（解析器返回序，非验证过的页面实际排名） |
| is_sponsored | 是否广告位 | `null` |

### 2.7 `stats` 与 `storage`

- **`stats`**：社交平台兼容槽位（views/likes/comments/shares/collections），Amazon 场景全 null。设计意图：同一套分析代码可同时吃 TikTok 视频记录与 Amazon 商品记录。
- **`storage`**：采集侧运行时簿记，消费方**只读**：`profile`（采集配置指纹）、`snapshot_id`（本次采集快照 ID，可做幂等去重）、`last_valid_price`（最近一次有效价格 + 观测时间，用于断采期间价格兜底）。

---

## 3. 消费方对接要点

1. **快照语义**：一条记录 = 一次采集。同一 ASIN 多次采集是**多条记录**（按 `fetched_at` 区分），价格/BSR/评分趋势靠多快照差分——与 TKCrawler 4 张快照表 `(实体ID, snapshot_date)` 的设计一致，入库时 `snapshot_date = fetched_at 的日期部分`。
2. **先查门控再用数**：`collection_conditions.status = unconfirmed` 或 `comparison_ready = false` 的记录，**不得**用于跨商品比较（本样例即因变体绑定未验证而不可比）；`region_verified = true` 仅代表地区条件达标。
3. **变体注意**：`external_id`（B0DLQ8MVF7）是父 ASIN，实际选中变体是 `observed.size_option_asin`（B0752XJYNL）。按 ASIN 聚合时先明确用哪个 ID。
4. **价格口径**：`price` 永远是展示价；定制商品（POD 类目全是）最终成交价 ≠ 展示价。利润测算时注意。
5. **`target_status_code = partial` 是常态**：解析失败的字段为 null，且有 `evidence.price_missing_reason` 这类解释字段，不要因部分缺失丢弃整条记录。
6. **原文与结构化并存时以结构化为准**：`best_sellers_rank_raw` / `date_first_available_raw` 仅审计用。
7. **可复现性**：`relevance.rule_version` + `evidence.import_source`（本地 HTML）保证判定可回溯重跑。
8. **交接脱敏项**：`evidence.import_source`（本机路径）清空或脱敏后再外发。

---

## 4. 与统一数据库的映射建议

| 记录字段 | 建议去向 |
|----------|----------|
| 整条 JSON | 新表 `amazon_product_snapshots`（主键 `(external_id, snapshot_date)`，JSONB 存原始记录 + 抽列建字段），复用快照差分模型 |
| commerce.price / rating / rating_count / best_sellers_ranks[0] | 抽为独立列（价格带、BSR 排序需要 SQL 查询） |
| collection_conditions.status / comparison_ready | 抽为独立列，查询侧过滤不可比记录 |
| relevance.status / product_scope / rule_version | 抽为独立列，选品漏斗过滤 |
| stats / author_id / hashtags / published_at | 不入库列，留在 JSONB 中（Amazon 恒空） |
| storage.snapshot_id | 可作为采集去重键 |

> 抽列原则：**要进 SQL WHERE/ORDER BY 的抽列，其余留 JSONB**，避免为每个字段建列导致 schema 膨胀。

---

## 附录：格式验证

```bash
# schema 已对真实样例验证通过（jsonschema draft-07）
python -c "
import json, jsonschema
schema = json.load(open('docs/schemas/amazon-product-record-v1.schema.json'))
instance = json.load(open('amazon-product-example.json'))  # 样例路径
jsonschema.validate(instance, schema)
print('VALID')
"
```

关键文件：
- 机读 Schema：`docs/schemas/amazon-product-record-v1.schema.json`
- 样例：用户提供（WeChat 拖拽文件，amzpy 产出，B0DLQ8MVF7）
