# TikTok Hashtag（话题页）接口逆向分析 — Agent G

> 存档日期：2026-10-07
> 分析方式：**离线假设分析**（offline-sample，无 live 取证）
> 结论状态：**全部条目 verified: false**，等待实机校准

## 1. 路由与授权门（reverse-skill-router 契约）

| 项 | 值 |
|---|---|
| 路由结果 | R3「JS / frontend reverse」→ PRIMARY skill `js-reverse` |
| 路由脚本 | `scripts/master-route.sh`（命中后按 routing.json R3 取 `js-reverse/SKILL.md`） |
| 授权门 | **未 granted**：本任务对 tiktok.com 无 live 抓取授权，按 scope 契约禁止对目标 ACT |
| 执行模式 | js-reverse 五阶段工作流的 Observe/Capture 阶段降级为**离线分析**：基于公开知识整理假设结构，全部标记 `verified: false`，实机校准前置为必须步骤 |

js-reverse 工作流对照：

| 阶段 | 状态 | 说明 |
|---|---|---|
| Observe（页面观察/网络取证） | 未执行 | 无 live 授权 + 用户纪律"测试全 mock，禁止真实请求" |
| Capture（XHR/签名采样） | 未执行 | 同上 |
| Rebuild/Patch（补环境复现） | 不适用 | 无运行时证据可复现 |
| DeepDive（去混淆） | 降级 | 本任务仅需数据结构假设，无需还原签名算法 |
| 产出 | 已完成 | 本文 + `src/scrapers/hashtag_selectors.json`（全部假设初值） |

## 2. Evidence → Finding → Path

### E-1 话题页 URL 形态（公开常识）

- **Evidence**：TikTok Web 端话题页对外地址长期为 `https://www.tiktok.com/tag/{tag_name}`；App 分享链接常见 `/discover?tag=` 变体。
- **Finding**：Web 端爬取入口以 `/tag/{tag}` 为候选主模板。
- **Path**：`hashtag_selectors.json → urls.tag_page`
- **verified**: false（未实机确认；注意 tag 需 URL 编码，去掉 `#` 前缀）

### E-2 话题数据内嵌节点（Next.js 水合结构）

- **Evidence**：TikTok Web 各页面（视频页、Shop 页）均以 `<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">`（新）或 `<script id="SIGI_STATE">`（旧）承载水合 JSON；本仓库商品线（Agent D）已按此假设建 fixture。
- **Finding**：话题页大概率沿用同一水合机制；话题详情历史上挂在 `__DEFAULT_SCOPE__["webapp.challenge-detail"]`（TikTok 内部将话题称为 challenge）。
- **Path**：`hashtag_selectors.json → embedded_json`（universal_data / sigi_state / root_candidates）
- **verified**: false

### E-3 总播放量字段（views_total）

- **Evidence**：SIGI_STATE 时代结构为 `challenge.challengeInfo.stats.viewCount`（ challenge 即话题对象）；话题页 UI 上有 `data-e2e="challenge-views-count"` 类的播放量展示位（带 k/M/B 后缀的展示文本）。
- **Finding**：候选 JSON 路径 `challengeInfo.challenge.stats.viewCount`（数值）→ 展示文本可能为 `"61.7B"` 形式，解析器必须处理 k/M/B 后缀。
- **Path**：`hashtag_selectors.json → fields.views_total`；解析器 `HashtagParser.parse_view_count()`
- **verified**: false

### E-4 话题名字段（tag_name）

- **Evidence**：话题对象历史上含 `challengeName` / `title` 字段；页面 `<h1 data-e2e="challenge-title">` 展示 `#tag`。
- **Finding**：以 URL 传入的 tag_name 为准，页面字段仅作交叉校验（与商品线 product_id 同策略：URL 参数获胜）。
- **Path**：`hashtag_selectors.json → fields.tag_name`
- **verified**: false

### E-5 Top 视频列表（get_top_videos）

- **Evidence**：话题页首屏嵌入 JSON 历史上含 `itemList` / `ItemModule`（视频对象集合，video id 为 15-19 位纯数字串）；分页走 XHR `api/challenge/item_list`，需要 `msToken`/`X-Bogus` 类签名参数（TikTok Web 风控）。
- **Finding**：
  1. 初版从话题页内嵌 JSON 提取视频 ID（列表上限受首屏数量限制），CSS 回退抓 `a[href*='/video/']` 锚点 + 正则提取；
  2. `api/challenge/item_list` 签名链路（X-Bogus/msToken）**超出本轮范围**，留待 js-reverse live 校准后再启用。
- **Path**：`hashtag_selectors.json → top_videos`；`urls.api_challenge_item_list`（仅存档占位，爬虫未使用）
- **verified**: false

### E-6 风控面（与仓库反检测基建对齐）

- **Evidence**：仓库 BaseScraper 已实现 429/403 软冷却、随机间隔、日配额；anti_detection 提供 curl_cffi/camoufox 双通道。
- **Finding**：话题采集全部走 `BaseScraper.fetch` 管线；话题页属公开页（无登录态通常可见播放量），初版走 curl 主通道即可。
- **Path**：`hashtag_scraper.py`（继承 BaseScraper，无自有请求逻辑）
- **verified**: false（通道选择需实机验证）

## 3. 候选端点清单（全部未验证）

| # | 端点 | 方法 | 用途 | verified |
|---|---|---|---|---|
| 1 | `https://www.tiktok.com/tag/{tag_name}` | GET | 话题页 HTML（内嵌 JSON 主数据源） | false |
| 2 | `https://www.tiktok.com/api/challenge/detail/?challengeName={tag}` | GET | 话题详情 JSON API（需签名参数，初版不用） | false |
| 3 | `https://www.tiktok.com/api/challenge/item_list/?challengeID={id}&count=30` | GET | Top 视频 JSON API（需 msToken/X-Bogus，初版不用） | false |

## 4. 数据契约与分工边界

- 爬虫产出 `{tag_name, views_total, snapshot_date}`，写入 `hashtag_snapshots` 表（`SnapshotDiffEngine.save_snapshot(data, table='hashtag_snapshots')`）。
- `views_growth_7d` **不在爬虫内计算**：由 `SnapshotDiffEngine.calculate_growth` 对两次快照差分得出。
- 增量校准：`views_total` 为 int（次），展示文本（"61.7B"）由 `parse_view_count` 归一化。

## 5. 实机校准步骤（后续执行 js-reverse live 流程时）

1. 授权 granted 后，用浏览器打开 `https://www.tiktok.com/tag/tiktokmademebuyit` 保存完整 HTML；
2. 检查 `__UNIVERSAL_DATA_FOR_REHYDRATION__` / `SIGI_STATE` 中 `webapp.challenge-detail` 是否存在，对照 `challengeInfo.challenge.stats.viewCount`；
3. 用 `js-reverse_list_network_requests` 观察首屏后的 item_list XHR，采样签名参数；
4. 逐条目把 `hashtag_selectors.json` 中 `verified` 改为 `true` 并注明验证日期。

## 6. 建议下一步（选一个编号）

1. live 授权后按第 5 节执行 js-reverse 实机校准，回填 verified
2. 深入分析 item_list 的 X-Bogus/msToken 签名链路
3. 用 anything-analyzer 对话题页做一次抓包对照 E-2/E-3 假设
4. 暂停逆向，先联调 SnapshotDiffEngine 写入 hashtag_snapshots

---

## 7. 实测证据章节（Agent O，2026-10-07）

首次真实请求校准，通道：游客态 curl_cffi chrome131 + 出口代理 127.0.0.1:7892。
完整发现：docs/probe/probe-findings-2026-10-07.md。

### E-7 /tag/webdev 游客态实抓（status 200）

- 证据：docs/probe/20261007T144849Z_tag_webdev_try1_body.html（原始 200KB 截断）、
  20261007T144849Z_tag_webdev_try1_embedded___UNIVERSAL_DATA_FOR_REHYDRATION__.json（完整 260KB）。
- 内嵌节点 `__UNIVERSAL_DATA_FOR_REHYDRATION__` 存在且可完整解析，SIGI_STATE 不存在。
- **E-7.1 证伪**：payload scope 仅 seo.abtest / webapp.a-b / webapp.app-context /
  webapp.biz-context / webapp.i18n-translation 五键——无 webapp.challenge-detail、
  challengeInfo、challenge（全文 0 次命中）。§2/§3 的 challenge JSON 路径在游客
  curl_cffi 通道**不可用**。
- **E-7.2 证伪**：DOM 为空壳（`<div id="app">` 空挂载），无
  h1[data-e2e='challenge-title']、无 strong[data-e2e='challenge-views-count']、
  无 a[href*='/video/']，head 仅 3 个标签（无 SEO meta 兜底）。
- E-7.3：views_total 需走签名 API（`api/challenge/detail`）或浏览器渲染通道；
  初版 curl_cffi 游客通道下 HashtagParser 对真实页面优雅返回 None/[]（实测）。
- WAF 表现：200 正常、无验证码/403/429/418；正常页资源清单含 captcha-*.js 文件名，
  WAF 检测不可对正文裸词匹配 "captcha"。

### 对选择器文件的影响

hashtag_selectors.json 全部 challenge 相关条目在本通道证伪/不可用，note 已附证据；
verified 标志因测试守卫（tests/test_hashtag_scraper.py
test_selectors_all_marked_unverified）约束暂保持 false，翻转补丁见
docs/probe/probe-findings-2026-10-07.md §4/§5。

---

## 8. 实测证据章节 II：签名 API 首次实机验证（Agent P，2026-10-07）

> 路由：reverse-skill-router R3（js-reverse）→ 本节为 Rebuild 工作流的"请求重放"验证腿。
> 通道：TikTokSigner（纯 Python X-Bogus）+ curl_cffi chrome131 + 代理 127.0.0.1:7892，
> 游客态。探针脚本 scripts/probe_signed_api.py，预算 6/10 次收手。
> 完整发现：**docs/probe/probe-signed-api-findings-2026-10-07.md**（本节为摘要）。

### E-8 X-Bogus 实机有效性：证实 ✅

- **Evidence**：GET /api/challenge/detail/?challengeName=webdev&…&X-Bogus=…（TikTokSigner
  签名，dummy msToken，无真实 Cookie）→ 200 + statusCode=0 +
  challengeInfo.challenge{id,title,stats} 全量数据（id=17603590，viewCount=739500000）。
  证据 docs/probe/20261007T154904Z_api_challenge_detail_body.json / _meta.json。
- **Finding**：signer.py 的 X-Bogus 算法被服务端接受（此前 docs/reverse/video-api.md 标注
  "assumed, pending on-device verification" 的状态解除）。被证实的请求形状：X-Bogus 以
  query 附加 + UA 绑定（Chrome/131 Windows）+ 完整基参 + 游客 dummy msToken（query 与
  Cookie 同时出现）。
- **Path**：src/scrapers/signer.py（无需修改）；hashtag_selectors.json
  urls.api_challenge_detail → **verified: true**；新增字段条目见 E-8.1。

### E-8.1 结构修正：challengeName 不存在，真实字段是 title

- **Evidence**：同上响应体——challenge 子树键为 id/title/desc/stats/isCommerce 等，
  无 challengeName；精确播放量在 challengeInfo.statsV2（字符串）。
- **Finding**：原假设 E-4 的 `challenge.challengeName` 证伪；selector fields.tag_name 已
  修正为 `challengeInfo.challenge.title` 并 verified:true；fields.views_total
  verified:true（challenge.stats.viewCount 主路径），新增 fields.challenge_id
  （challengeInfo.challenge.id，item_list 分页输入）。
- **Path**：hashtag_selectors.json → fields.*；测试守卫升级为
  "verified:true 必须携带 docs/probe/ 证据引用"（test_selector_evidence_discipline）。

### E-8.2 通道状态矩阵（本轮结束时的真实边界）

| 端点 | 状态 | 门控 |
|---|---|---|
| /api/challenge/detail/ | ✅ 游客可用（X-Bogus + dummy msToken） | 无 |
| /api/challenge/item_list/ | ❌ 200 空体（4 种参数组合复现） | 深层风控：疑似 X-Gnarly 头 / web_id+verifyFp / 会话指纹（§下轮输入） |
| /api/post/item_list/ | ❌ 200 空体（同上） | 同上 |

- **Finding**：空体 200 ≠ 签名失败（同组合下 detail 放行）——业务层静默拒绝，
  X-Bogus 链路本身有效。数据缺口：hashtag 视频列表、用户视频列表（avg_views_30d 样本）
  仍不可得。
- **Path**：src/scrapers/hashtag_scraper.py 新增 scrape_hashtag_api（已打通 views_total
  游客通道）；parsers/hashtag_parser.py 新增 parse_hashtag_api；tests 新增
  TestHashtagParserApi / TestScrapeHashtagApi（fixture:
  tests/fixtures/hashtag_api_probe_webdev_detail.json）。

### 下轮逆向输入（按优先级）

1. 浏览器内 hook（js-reverse Observe/Capture）采集 item_list XHR 完整请求，消融定位
   门控条件（X-Gnarly？web_id/verifyFp？）。
2. 真实 web_id 复测（`probe_signed_api.py single … --cookies`，1-2 次预算）。
3. views_total 已可用，建议先把 scrape_hashtag_api 接入 SnapshotDiffEngine 日常链路。

---

## 9. 实测证据章节 III：浏览器被动捕获 + X-Gnarly 消融（Agent R，2026-10-08）

> 路由：reverse-skill-router R3（js-reverse）→ Observe/Capture 阶段（jshookmcp 不可用，
> 按 skill 回退原则改用 playwright + 系统 Chrome channel='chrome'，headless）。
> 方式：**纯被动捕获**——页面自身对 /api/ 发起的 XHR（request headers + response body），
> 探针脚本 scripts/probe_itemlist_hook.py 全程打印请求清单，证据落 docs/probe/。
> 通道：系统 Google Chrome 154（headless）+ 出口代理 127.0.0.1:7892，游客态。
> 风控纪律执行：tag 页加载 3/3（含 2 次本地启动故障后重试；无 403/429/418/验证码），
> curl_cffi 重放 1/2，之后主动收手。

### E-9 捕获请求清单（20261008T025405Z 会话，tag=webdev）

页面自身发起的关键 XHR（全部 200，无 WAF 动作）：

| 请求 | 状态 | 响应体 | 证据 |
|---|---|---|---|
| GET /api/challenge/detail/?challengeName=webdev&…（32 参数） | 200 | 716B（challengeInfo 完整数据） | `20261008T025405Z_api_challenge_detail?challengeName=webdev_body.json` + `_meta.json` |
| GET /api/challenge/item_list/?challengeID=17603590&cursor=0&count=30&…（36 参数） | 200 | **0B** | `20261008T025405Z_api_challenge_item_list?challengeID=17603590&cursor=0&count=30_body.txt` + `_meta.json` |
| POST /tiktok/ppf/api/eligibility/v2 | 200 | 206B | 同会话目录 |

item_list 在页面水合后 ~2s 自动发起一次（cursor=0），40s 观察窗 + 2 次滚动内
**无重试、无第二页**；同会话 detail 立即返回业务数据。

### E-9.1 X-Gnarly 消融：存在，但载体是 query 参数而非请求头

- **Evidence**：浏览器发出的 detail 与 item_list URL 中均带
  `X-Gnarly=MRR6HgJMeU6N-…` / `MO1h1D7GxQdG9ZRbV5ut-…`（48 字符，随请求变化）；
  请求头集合（playwright 可见面）只有标准 sec-ch-ua/UA/referer/accept-language。
  证据：`20261008T025405Z_api_challenge_*_meta.json`（cookie 值截断）。
- **Finding**：Agent P 假设方向正确（X-Gnarly 是 2025 下半年新签名链），但它是
  **query 参数**，与 X-Bogus 并列，由页面 JS 生成；纯 Python TikTokSigner 目前
  只产 X-Bogus，这是签名侧的真实缺口。
- **Path**：hashtag_selectors.json → urls.api_challenge_item_list（note 更新，
  verified 保持 false）。

### E-9.2 决定性消融：门控不在签名头/设备参数，而在会话层

浏览器请求已具备此前假设的全部缺失项，**item_list 依旧 200 空体**：

| 此前假设的缺失项 | 浏览器实测 | 结果 |
|---|---|---|
| X-Gnarly | ✅ query 携带（JS 实时生成） | 仍 0B |
| X-Bogus | ✅ query 携带 | 仍 0B |
| web_id / device_id | ✅ `device_id=7694123832713479698`（服务端签发）+ `WebIdLastTime` | 仍 0B |
| 真实 msToken | ✅ 服务端 Set-Cookie 签发（168ch，query/cookie 双通道机制由页面管理） | 仍 0B |
| ttwid / tt_chain_token / tt_csrf_token Cookie 罐 | ✅ 完整（证据截断存档） | 仍 0B |
| clientABVersions / verifyFp 类 AB 参数 | ✅ item_list 特有 36 参数（含 clientABVersions=70508271,…） | 仍 0B |

同会话、同 Cookie、同签名链下 challenge/detail 放行（716B 数据）。

- **Finding**：item_list 的业务层门控条件**不在**请求形状（头/参数/签名），
  而在于会话属性——最可能是：①游客（无登录态）直接拒绝列表类数据；
  ②环境风险分（headless Chrome / CDP 特征 / MacIntel+Win-UA 混合）触发静默拒绝；
  ③IP/数据中心信誉（代理出口 region=JP）。纯"补齐请求形状"路线（含纯 Python
  产出 X-Gnarly）**已被证伪为不充分条件**。
- **Path**：见本节末"下一轮决策输入"。

### E-9.3 重放验证（curl_cffi chrome131 + 完整捕获 URL + 浏览器 Cookie 罐）

- **Evidence**：捕获后数秒内同会话重放 1 次（复用原始 URL 含 X-Gnarly/X-Bogus/
  device_id，10 cookie 870 字符罐）→ **200 + 0B**。
  证据：`20261008T025405Z_replay_api_challenge_item_list_meta.json` / `_body.txt`。
- **Finding**：重放不可行——且由于浏览器源请求本身即空体，本实验与 E-9.2 合并
  得出一致结论：请求形状复刻（无论浏览器还是 Python）对 item_list 无效，
  "浏览器 cookie+签名头移植 curl_cffi"路线关闭。
- **Path**：无代码落地（数据通道未打通，按守卫纪律不新增 scrape_hashtag_videos、
  不新增 fixture 用例）。

### 对选择器文件的影响

- `urls.api_challenge_item_list` verified **保持 false**，note 追加 E-9 证据
  （端点形状完整存档：36 参数 + clientABVersions + X-Gnarly query 签名）。
- 其余条目不变；无新 fixture（未获得 itemList 真实响应）。

### 下一轮决策输入（按预期收益排序）

1. **登录态通道**：E-9.2 指向会话属性门控；用仓库既有 camoufox 登录状态机
   （anti_detection/browser_client.py）取得登录 Cookie 后复测 item_list
   （浏览器被动捕获 1 次 + 重放 1 次即可消融）。
2. **headful/真指纹复测**：同会话改 headless=False + 指纹一致（UA 与
   browser_platform 匹配，本次 MacIntel vs Win-UA 不一致是风险分噪音源），
   排除环境风险分假设（浏览器预算 1 次）。
3. **X-Gnarly 算法逆向**：仅在 1/2 排除后才有意义——若登录态浏览器能出数据而
   Python 不能，则需 hook 页面 JS 产出 X-Gnarly（js-reverse Rebuild 工作流）。
4. 第三方签名服务（风险与合规自评后决定）。
