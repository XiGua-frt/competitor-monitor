# 达人主页 Web API 逆向分析（Agent F）

> 目标：TikTok 用户（达人）主页 Web 端数据获取路径分析。
> 纪律：本文档中所有未经实机验证的结构均为【假设】，与已验证逻辑严格分离披露；
> 每个条目的验证状态必须与 `src/scrapers/creator_selectors.json` 中的
> `verified` 字段保持一致。
> 最后更新：2026-10-07（Agent F）

---

## 0. 作业记录（reverse-skill-router 路由链）

| 步骤 | 结果 |
|------|------|
| 路由 | `master-route.sh`（R3 关键词命中：Web API / 页面嵌入 JSON / 前端数据链路）→ PRIMARY = `js-reverse` |
| PRIMARY 加载 | router 包内 `js-reverse/SKILL.md` 缺失（目录为空），改用独立 skill `~/.workbuddy/skills/js-reverse/SKILL.md`（已全文加载） |
| case-init | **偏离**：本任务写集白名单不含 `work/` 目录，scope 以本节（§0）代替 `scope.md` 落档 |
| 执行面 | 本环境无 `js-reverse_*` MCP 工具面，且任务纪律**禁止真实请求**，故 Observe/Capture 动态阶段未执行，改走静态/文档化分析（详见 §5 偏离说明） |

### Scope（代替 scope.md）

- **目标范围**：TikTok 用户主页（`https://www.tiktok.com/@{unique_id}`）及其橱窗入口；
  提取字段限 CREATOR_FIELDS 六项 + 橱窗商品 ID 列表。
- **授权状态**：offline-sample preset。全部测试走本地 fixture，无任何对 TikTok
  的真实 ACT（请求）。auth 状态：**未 granted 实机采集**，后续实机校准前须单独审批。
- **样本**：`tests/fixtures/creator_profile_sample.html`、
  `tests/fixtures/creator_showcase_sample.html`（均为按假设结构自建的离线样本，
  非真实页面快照——见 §5 偏离说明）。

---

## 1. 数据获取路径总览（Evidence → Finding）

### Evidence（公开知识来源，非实机抓包）

- E1: TikTok Web 为 React/Next.js 混合栈，页面数据通过 `<script>` 内嵌 JSON
  首屏水合（hydration）注入——与本仓库 Agent D 对 Shop 页的分析
  （`selectors.json` `_meta`）同源。
- E2: 社区开源实现（davidteather/TikTok-Api、drawrowfly/tiktok-scraper、
  Evil0ctal/Douyin_TikTok_Download_API，见仓库根 `tk-crawler-github-references.md`）
  长期依赖两个内嵌节点：`__UNIVERSAL_DATA_FOR_REHYDRATION__`（新版）与
  `SIGI_STATE`（旧版）。
- E3: 上述开源实现对直连 API 端点的调用需携带 `X-Bogus` / `_signature` /
  `msToken` 等签名参数，签名算法随版本月级变动。

### Finding（结论）

- F1: **首选路径 = 主页 HTML 内嵌 JSON**，不做签名端点直连。理由：签名算法
  （E3）维护成本高且月级失效；内嵌 JSON 与页面同源返回，复用现有
  BaseScraper + AntiDetectionClient 通道即可。
- F2: 用户主页嵌入 JSON 的候选根（按优先级）：
  1. `__UNIVERSAL_DATA_FOR_REHYDRATION__` → `__DEFAULT_SCOPE__["webapp.user-detail"]`
  2. `SIGI_STATE` → `UserModule.users`（键为 uniqueId 的字典）+
     `ItemModule`（视频项字典）
- F3: 橱窗数据不在主页首屏 JSON 中稳定暴露（E1/E2 均未给出可靠路径），
  假设独立橱窗页 `/@{unique_id}/showcase`，商品 ID 从链接正则提取
  （与 Agent D 类目页做法对齐）。

---

## 2. 用户主页端点契约【假设 · verified: false】

### 2.1 页面 URL

```
https://www.tiktok.com/@{unique_id}
```

- 假设 `creator_id` 入参即 `unique_id`（页面 handle）。若传入纯数字 ID
  （uid），页面 URL 无法由 uid 直接构造，解析器仍以 JSON 内的
  `user.id`（uid）回填 `creator_id` 字段，URL 构造失败由调用方处理
  （见 parser 的 creator_id 优先级）。

### 2.2 内嵌 JSON 字段映射（假设路径，均未实机验证）

| 目标字段 | UNIVERSAL_DATA 假设路径（相对 root） | SIGI_STATE 假设路径 | CSS 回退（假设） |
|---|---|---|---|
| creator_id | `user.id` | `UserModule.users[*].id` | `[data-e2e='user-info'] [data-e2e='user-link']` 的 href 中 `/@x` 段 |
| nickname | `user.nickname` | `UserModule.users[*].nickname` | `h1[data-e2e='user-title']` |
| follower_count | `stats.followerCount` | `UserModule.users[*].stats.followerCount` | `[data-e2e='followers-count']`（"1.2M" 类文本，需后缀解析） |
| product_count | `commerceInfo.showcaseItemCount` | 无（缺失时仅 CSS 回退） | 计数 `div[data-e2e='showcase'] a[data-e2e='product-card']` |
| bio（供 category_tags） | `user.signature` | `UserModule.users[*].signature` | `[data-e2e='user-bio']` |
| 视频列表（供 avg_views_30d / 类目） | `itemList[*].stats.playCount`、`itemList[*].desc` | `ItemModule[*].stats.playCount`、`ItemModule[*].desc` | 无（首屏外需 XHR，见 §4） |

root 候选（`creator_selectors.json` `embedded_json.root_candidates`）：

1. `__DEFAULT_SCOPE__['webapp.user-detail'].userInfo`
2. `UserInfoModule.userInfo`（SIGI_STATE 变体）
3. `UserModule.users`（SIGI_STATE 键值字典，取首个/匹配项——特殊处理，
   不走通用 walk）

> 注意：`webapp.user-detail` 下是否直接是 `userInfo` 还是别的包裹层
> **未经实机验证**；`root_candidates` 机制保证实机校准时只需在 JSON 中
> 改一行路径。

### 2.3 橱窗端点【假设 · verified: false】

```
页面: https://www.tiktok.com/@{unique_id}/showcase
商品 ID 提取: 链接正则 (?:/product/|product_id=)(\d{6,})   ← 与 shop 侧类目页共用假设
```

E2 中的开源项目提示橱窗/带货列表多为登录态 + XHR 分页（`/api/commerce/...`），
公开匿名通道能否翻页**未验证**。首版只做首屏提取，翻页留待实机校准。

---

## 3. avg_views_30d 估算方法论

**主页接口不直接提供近 30 天平均播放量**（E2：公开嵌入 JSON 无此聚合字段）。
按任务卡要求实现估算：

1. 从内嵌 JSON 提取主页可见视频的 `stats.playCount`（首屏通常 ≤ 10-35 条，
   无需额外请求即满足"最近 30 个"上限——主页视频流按时间倒序排列【假设】）；
2. 取**最多 30 条**、按页面顺序（即最新在前）；
3. 取**中位数**（statistics.median；偶数条取中间两值均值，四舍五入为 int）；
4. 视频列表缺失/为空 → `avg_views_30d = None`（不猜测，与快照表
   `Integer NULL` 语义对齐）。

局限披露：中位数基于首屏可见视频，若实机首屏 < 30 条且无水合分页数据，
估值偏低偏差无法消除——校准时记录实际首屏条数。

---

## 4. category_tags 提取规则

`bio`（user.signature）+ 采样视频 `desc` 中的 `#tag`：

1. 正则 `#([\w\u4e00-\u9fff]+)` 提取（含中文标签）；
2. bio 标签在前、视频标签在后（页面顺序），去重保序；
3. 上限 10 个，逗号拼接为单字符串（对齐 `creator_snapshots.category_tags`
   String(500) 列）；
4. 全空 → None。

视频"类目"在公开接口中无结构化分类字段，hashtag 即类目代理【披露：近似】。

---

## 5. 已验证 vs 假设（分离披露）

### 已验证（本仓库内可复现）

- [V] 解析 LOGIC：对自建 fixture 全部通过单测（`tests/test_creator_scraper.py`）。
- [V] `_walk_json_path` 语义、后缀数字解析（复用 `product_parser.parse_number_with_suffix`）。
- [V] BaseScraper 管线行为：配额门、429/403 软冷却（复用基建，Agent D 已验证同类）。
- [V] 内嵌 JSON 双节点回退机制（UNIVERSAL_DATA → SIGI_STATE）的代码路径。

### 假设（verified: false，实机校准前不得当真）

- [A] 主页/橱窗 URL 模板（§2.1、§2.3）。
- [A] `webapp.user-detail` / `UserModule` 嵌入 JSON 全部字段路径（§2.2）。
- [A] `data-e2e` CSS 回退选择器。
- [A] 橱窗商品 ID 正则与首屏可提取性。
- [A] 主页视频流按时间倒序、首屏含 playCount。
- [A] `avg_views_30d` 可由首屏视频估算（§3 局限）。

### 实机校准清单（auth granted 后执行）

1. 保存真实主页 HTML，确认内嵌节点 ID 与 root 路径，更新 selectors 并置 verified:true；
2. 用 20 个达人人工抽查 follower_count / product_count（对应任务卡验收标准）；
3. 记录首屏视频条数与 playCount 可得性，修正 §3 估算说明；
4. 确认橱窗首屏商品数与翻页形态，决定是否需要登录态通道（SessionManager）。

---

## 6. 偏离说明

| 偏离 | 原因 |
|------|------|
| case-init 的 `scope.md` 未落 `work/` 目录 | 写集白名单不含 `work/`；scope 内嵌本文 §0 |
| js-reverse 五阶段工作流只完成静态分析（无 Observe/Capture 动态证据） | 本环境无 `js-reverse_*` MCP 工具面 + 任务纪律禁止真实请求 |
| 字段级 `verified` 全部保持 false | 无实机页面可对照；只有人工实机校准后才允许置 true |
| `filter_mid_tier` 命名对应"腰部(1-10万)"，而 tier 枚举名为 `micro` | 任务卡两处命名不一致；代码中以 docstring 显式声明映射关系 |

---

## 7. 实测证据章节（Agent O，2026-10-07）

首次真实请求校准，通道：游客态 curl_cffi chrome131 + 出口代理 127.0.0.1:7892。
完整发现：docs/probe/probe-findings-2026-10-07.md。

### E-7 /@khaby.lame 游客态实抓（status 200）

- 证据：docs/probe/20261007T144901Z_creator_khaby_lame_try1_body.html、
  20261007T144901Z_creator_khaby_lame_try1_embedded___UNIVERSAL_DATA_FOR_REHYDRATION__.json（完整 263KB）。
- **E-7.1 证实**：`__DEFAULT_SCOPE__['webapp.user-detail'].userInfo` 根路径命中
  （含 user / stats / statsV2 / itemList 键）；SIGI_STATE 不存在。
- **E-7.2 证实**（字段路径与 §2.2 假设一致）：
  - user.id = '127905465618821121'（creator_id）
  - user.nickname = 'Khabane lame'
  - user.signature = 完整简介（bio）
  - stats.followerCount = 163000000（int、取整）；**新发现** statsV2.followerCount
    = "162998174"（字符串、精确值，后续可切换）
- **E-7.3 缺口**：userInfo.itemList 键存在但游客态 SSR 值为 `[]`——主页视频由客户端
  经签名 API 拉取，`avg_views_30d`/`category_tags`(视频文案部分) 在本通道恒不可得，
  解析器优雅降级 None（实测通过，tests/fixtures/creator_probe_khaby_lame.html）。
- **E-7.4**：DOM 空壳（无 data-e2e、无 /video/ 链接），CSS 回退在本通道全部不可用；
  product_count 路径在非电商账号上不可判定（实测 user.commerceUserInfo={"commerceUser": false}）。
- WAF 表现：200 正常、无验证码/403/429/418。

### 对选择器文件的影响

urls.profile_page、universal_data_script_id、root_candidates[0]、user.id、
user.nickname、stats.followerCount、user.signature 七条已获证据证实，note 已附证据
文件名；verified 标志因测试守卫（tests/test_creator_scraper.py
TestSelectorDiscipline）约束暂保持 false，翻转补丁见
docs/probe/probe-findings-2026-10-07.md §4/§5。
