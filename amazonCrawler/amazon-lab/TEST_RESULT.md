# 亚马逊美国站小规模实测

日期：2026-10-07，北京时间。独立环境：`解压目录\amazon-lab\.venv`。
上游：[theonlyanil/amzpy](https://github.com/theonlyanil/amzpy)，提交 `4c0ff9c`。

## 范围与结果

- 关键词：`custom pet portrait shirt`，目标是人穿的定制宠物肖像服装。
- 第一轮 Amazon.com 搜索自动跳转日本站；未保存日本站商品为美国站数据。
- 一次不跟随跳转的诊断确认 HTTP 302，Location 指向 Amazon.co.jp。
- 加入 `mr_donotredirect` 后，本轮一页搜索和三个详情最终均在 `www.amazon.com`，HTTP 200。
- 上游解析出 48 个搜索结果；详情只访问并保存前 3 个不同 ASIN。
- 48 是解析器返回条数，不保证与页面所有卡片逐一对应；上游会跳过部分广告容器。
- 三个详情 ASIN：B0DLQ8MVF7、B0GWVTMR4Q、B0H22J6F1B。
- 没有翻页、抓评论正文、登录、购物或运行第三方采集服务。

## 字段覆盖

| 字段 | 本轮结果 |
|---|---|
| ASIN、商品链接、标题 | 3/3 |
| 描述/产品要点、封面、品牌 | 3/3 |
| 评分、评分数量 | 3/3，分别 4.6/104、5.0/5、5.0/1 |
| 首次上架日期原文、BSR 原文 | 3/3，保存在 commerce 中；未冒充精确发布时刻或销量 |
| 主商品价格及币种 | 1/3，第一条显示 JPY 2370；没有换算或冒充美元 |
| 卖家 ID、近期购买量、文字评论数量 | 未取得，留空 |
| 完整图片/视频列表 | 未取得，仅保存封面链接，完整性标记 false |
| 社交互动指标 | 不适用，全部留空；评分数量不是评论正文数量 |
| 搜索位置、广告标识 | 仅记录解析器返回顺序，真实页面排名与广告标识未验证 |

第二、三条上游全页解析器返回 JPY 1542、2133，但未在主商品价格区确认，
为避免推荐商品或其他位置的价格混入，统一输出的 price/currency 留空。
当前站点是美国站，但地区偏好仍显示日元；美元价格与美国配送地址尚未验证。

按标题初审，第一、二条明确涉及宠物照片/肖像定制；第三条是通用照片/文字定制 T 恤，
仍需进一步筛选宠物肖像相关性。三条均不代表热度前三或销量趋势。

## 本地适配与限制

- 上游 Session 初始化移除隐式首页请求与 Cookie 打印。
- MAX_RETRIES=0，单轮预算 4 次逻辑请求，固定 User-Agent，请求间隔 15 秒。
- 不使用代理轮换；系统现有网络设置仍可能影响地区识别。
- 记录每次 HTTP 状态与最终域名；遇到非 200、验证页、登录页或跨站跳转即停止。
- 请求会跟随 HTTP 跳转，逻辑请求数不等于所有实际 HTTP 交换数。
- 搜索和基础详情沿用 AmzPy；实验脚本补充描述、评分数量、首次上架日期和 BSR。
- 价格只从主商品价格容器取；评分数量来自明确标注 global ratings 的区域。
- 商品 HTML 保存在本地用于离线校对，未下载商品图片。
- 尚未接入主项目、跨运行缓存、累计预算、持久熔断或增量更新。
- 环境安装与导入通过；真实请求和产物字段已检查。不是长期稳定性验证。

## 文件与运行方式

结果目录：`F:\shop\crawler-lab\test\amazon\amzpy`。

- `posts.json`：统一字段的三条商品。
- `upstream-details.json` / `search-results.json`：上游解析数据。
- `report.json`：成功一轮的域名、状态与数量。
- `report-before-region-fix.json` / `redirect-check.json`：地区跳转证据。
- `search.html` / ASIN 对应 HTML：用于离线校对；搜索页面已由成功美国站结果覆盖。

从 `F:\shop\crawler-lab` 运行，务必使用 -I 避免仓库同名目录干扰包导入：

```powershell
# 会发起真实网络请求，并覆盖当前实验结果
.\amazon-lab\.venv\Scripts\python.exe -I .\amazon-lab\probe.py

# 仅重读已保存的 HTML 校对字段，不发网络请求
.\amazon-lab\.venv\Scripts\python.exe -I .\amazon-lab\probe.py --normalize-saved
```

仍需优先解决美元显示和商品变体价格，再考虑接入长期采集。

## 2026-10-07 修复复测：美元已解决，配送地址暂停

### 原因确认

原页面 `glow-ingress-line2` 是 Japan，`currencyOfPreference` 是 JPY。
第二、三件主购买区明确提示不能配送到当前地址，所以缺价原因已确认是配送地区限制。
此前“可能需要选择变体”属于未验证的推测，不能作为本轮缺价结论。

### 修复

- HTTP 会话固定美元 `i18n-prefs=USD` 与英文偏好，不通过汇率换算伪造美元价格。
- 每件商品保存配送地区、页面币种偏好、可售状态及缺价原因。
- `us_preferences_verified` 同时要求页面有测试邮编 10001 和 USD；美国域名、美元显示不能单独证明美国配送偏好。
- 保持主商品价格范围限制，推荐商品价格继续忽略；显示价不是最终定制订单总价。
- 离线重读 HTML 保留原真实请求的采集时间，不改成离线重读时间。
- 使用旧搜索结果复测相同三个 ASIN，无需再次搜索；本轮只有三个商品详情逻辑请求。
- 新结果在 `test/amazon/amzpy/us-preferences-fixed/`，保留首轮数据。

### 结果

2026-10-07 18:45 北京时间，三次详情请求均为美国站 HTTP 200。

| ASIN | 主商品价格 | 页面币种偏好 | 配送地址 | 缺价原因 |
|---|---|---|---|---|
| B0DLQ8MVF7 | USD 14.99 | USD | Japan | 有显示价，仍未确认美国配送 |
| B0GWVTMR4Q | 空 | USD | Japan | delivery_location_unsupported |
| B0H22J6F1B | 空 | USD | Japan | delivery_location_unsupported |

已完成两项离线回归：推荐区域价格不冒充主商品价格并识别配送限制；USD 配合测试邮编才确认美国偏好。

### 美国配送设置的阻塞

新增独立浏览器设置脚本 `prepare_preferences.py` 尝试网站的配送地址弹窗。
浏览器页面命中了登录/验证检查，自动流程已停止，没有完成邮编设置或保存已确认的偏好会话。
用户选择“先只修能自动完成的部分”，因此暂停需要手动验证的地区设置，不继续重试。
只有后续完成美国测试邮编设置后，才能实测第二、三件是否显示有效报价。

本轮没有新增相关性算法、精确销量估算或长期采集系统，这些仍保持前述未完成状态。

```powershell
# 仅复测原有三个商品，写入 us-preferences-fixed 子目录
.\amazon-lab\.venv\Scripts\python.exe -I .\amazon-lab\probe.py --same-products

# 离线重新归一化本轮 HTML
.\amazon-lab\.venv\Scripts\python.exe -I .\amazon-lab\probe.py --normalize-fixed

# 不联网的地区与价格语义测试
.\amazon-lab\.venv\Scripts\python.exe -I .\amazon-lab\test_preferences.py
```

## 2026-10-08：美国配送偏好接入并复测成功

用户在采集专用 Chrome 中完成网页设置。页面确认 New York 10001，美元偏好 Cookie 为 USD。
首页没有旧选择器对应的币种标签，原等待脚本因此未自动确认；通过只连接本任务专用窗口的
`check_open_preferences.py` 核对页面和美元偏好，将会话保存在本机 private/amazon-preferences.json。
没有读取普通 Chrome 的账号资料，未输出 Cookie 值。该本地会话不要发给别人。

首次等待脚本的 timeout 报告保留作为历史诊断；实际成功导出证据在 open-preferences-report.json。
现已修复等待脚本：主页缺少币种标签时，可用明确的 USD 偏好 Cookie 加 10001 页面位置确认，
相关离线回归先失败后通过。仍需美国位置证据，单独 USD Cookie 不算地区确认。

18:20 北京时间，使用保存的会话重读原三个商品，未重新搜索；三次详情均为 Amazon.com HTTP 200。

| ASIN | 页面主商品显示价 | 配送位置 | 可售状态 |
|---|---:|---|---|
| B0DLQ8MVF7 | USD 14.99 | New York 10001 | In Stock |
| B0GWVTMR4Q | USD 12.95 | New York 10001 | In stock，通常 3～4 天发货 |
| B0H22J6F1B | USD 24.99 | New York 10001 | In Stock |

三条详情的 currencyOfPreference 均为 USD、us_preferences_verified=true，缺价原因均为空。
由日本配送限制引起的本轮缺价已解决。价格是当时页面展示值，未执行定制、加购或确认最终订单金额。
统一数据和 HTTP 证据已校验，三项离线回归通过；没有扩展到长期运行或更大采集量。

最新产物仍在 test/amazon/amzpy/us-preferences-fixed/，其中 posts.json、report.json 和 HTML 已更新为本轮结果。
后续复测会继续使用本地会话；会话过期、地区偏好失效或验证码仍可能影响采集。
