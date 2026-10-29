# TikTok Web 视频流 API 逆向分析档案（Agent E）

> 归属：Agent E（视频流采集工程师）· 独占写文件
> 方法论：reverse-skill-router → R3 `js-reverse`（Observe → Capture → Rebuild）
> 作业模式：**离线分析**。本环境无浏览器/CDP 动态取证面（js-reverse_* / jshookmcp
> 不可用），故以公开开源实现交叉比对为主，所有结论按 Evidence → Finding → Path
> 组织，未实机验证处一律标注 **"假设，待实机验证"**。

---

## 1. 路由与执行记录

- router：`config/routing.json` 命中 R3（js / frontend reverse）。
  关键词：`X-Bogus`、`前端签名`、`接口逆向`。
- js-reverse 五阶段在本次的映射：
  - Observe/Capture（需浏览器）→ **降级为公开源码取证**（见 §3 参考源）；
  - Rebuild → 纯 Python 复现 `src/scrapers/signer.py`；
  - DeepDive（去混淆）→ 不适用（签名算法已被社区完整还原，无需处理混淆 JS）。
- auth 状态：未对 TikTok 实机发起任何请求（遵守测试纪律：全 mock/fixture）。

## 2. 目标端点清单

| 端点 | 用途 | 验证状态 | 来源 |
|---|---|---|---|
| `https://www.tiktok.com/api/shop/product/videos` | 商品关联视频列表（任务卡给定） | **verified: false，假设，待实机验证** | AGENT_TASK_CARDS.md Agent E 卡；未见于任何公开开源项目 |
| `https://www.tiktok.com/api/item/detail/` | 单视频详情（`itemId`/`aweme_id`） | 公开来源一致验证，未实机 | huuhuybn/X-Gorgon 示例、drawrowfly/tiktok-scraper |
| `https://www.tiktok.com/api/post/item_list/` | 作者主页视频流（`secUid`+`cursor` 翻页） | 公开来源一致验证，未实机 | davidteather/TikTok-Api、drawrowfly/tiktok-scraper |
| 商品详情页 HTML 内嵌 JSON（`__UNIVERSAL_DATA_FOR_REHYDRATION__` / `SIGI_STATE`） | 从商品页 rehydration 数据提取关联视频（备用路径） | 结构假设，待实机（与 Agent D selectors.json 同源假设） | 项目内 selectors.json 策略 |

**Finding E-1**：任务卡中的 `/api/shop/product/videos` 在 davidteather、drawrowfly、
armxe 等公开项目中均无记载，属**未验证端点**。VideoStreamScraper 默认使用它
（任务卡要求），但响应结构按假设构造并允许通过 `video_selectors.json` 替换；
同时实现"商品页内嵌 JSON"备用采集路径。

## 3. 参考源（Evidence）

| # | 源 | 内容 | 采信度 |
|---|---|---|---|
| R1 | github.com/davidteather/TikTok-Api（v7.x，2026 仍在维护） | **不**复现 X-Bogus：用 Playwright 会话直接加载 tiktok.com，浏览器内页面 JS 自动附加 X-Bogus/msToken 后拦截 `/api/` 请求 | 高（行为面） |
| R2 | github.com/armxe/tiktok-api → `Web/bogus.py`（2026-03 更新） | X-Bogus 纯 Python 完整实现：double-MD5 + RC4 + 移位 base64，类 `Signer` | 高（算法面） |
| R3 | gist yvbbrjdr/X-Bogus.py | 同一算法的早期独立实现（常量一致、magic 常量不同：`88,194,176,26` vs `536919696` 字节序差异） | 中（版本差异线索） |
| R4 | github.com/huuhuybn/X-Gorgon README | 算法文字描述：19 字节 payload、double-MD5、RC4 key [0,1,14]、magic 536919696、自定义 base64 表 `Dkdpgh4ZKsQB80/Mfvw36XI1R25-WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe=`、输出 28 字符；**必须配合有效 msToken Cookie** | 高 |

版本差异说明：R2/R3 均为社区对 webmssdk JS 的还原。R3（旧版）把 magic 常量
以硬编码 4 字节 `88,194,176,26`（0x58,0xC2,0xB0,0x1A）写死，R2（较新）用整数
`536919696`（0x1FFD6B50 大端 4 字节）且 checksum 初值/截取方式一致。
实现采 R2 结构，magic 常量做成可配置——**TikTok 更新 JS 时改配置即可，无需改代码**
（任务卡可注入要求的落点之一）。

## 4. X-Bogus 算法分析（Path）

输入：URL query string（`urlparse(url).query`，**不含** X-Bogus 自身）、
User-Agent、可选 POST body（GET 为空串）、当前 Unix 时间戳。

步骤（对应 `src/scrapers/signer.py` 实现）：

1. `md5_params = md5(md5(query).digest())`、`md5_data = md5(md5(body).digest())`：
   双重 MD5，各取 `digest()[-2:]` 两个字节。
2. `md5_ua = md5( base64( RC4(key=[0,1,14], ua) ) )`：UA 先 RC4 再标准 base64
   再 MD5，取末两字节。
3. 组装 salt_list（22 元素）：
   `[ts, MAGIC(536919696), 64, 0, 1, 14, md5p[-2:], md5d[-2:], md5ua[-2:]]`
   `+ ts 大端 4 字节 + MAGIC 大端 4 字节 + [checksum, 255]`；
   checksum 初值 64，对 `salt_list[3:]` 逐元素 XOR。
4. `filter`：按下标 `[2,4,6,8,10,12,14,16,18,20,3,5,7,9,11,13,15,17,19]` 抽 19 个。
5. `scramble`：19 元素按 `a,k,b,l,c,m,d,n,e,o,f,p,g,q,h,r,i,s,j` 交织成串
   （等价于 arr1[::2]+arr1[1::2] 的逆）。
6. `RC4(key=[255])` 加密该 19 字节串。
7. 前缀 `\x02\xff` 拼接得 21 字节 → 自定义 base64（表见 R4，无 `=`），
   输出 **28 字符** X-Bogus。
8. 请求 URL 追加 `&X-Bogus=<28字符>`。

**Finding E-2**：签名含当前时间戳，秒级变化，不可缓存复用；服务端存在时间
窗口校验（窗口大小未知——**假设，待实机验证**，社区经验约 3~5 分钟）。

**Finding E-3（假设，待实机验证）**：
- X-Bogus 与 User-Agent **绑定**（步骤 2），请求 UA 必须与签名 UA 一致；
- query 需包含 `msToken`（页面 Cookie 提取）参与签名，且 Cookie 同步携带；
  无有效 msToken 时即使签名正确也大概率返回空/403；
- 常用公共 query 基参：`aid=1988&app_language=zh-CN&device_platform=web_pc`
  等（公开项目常见取值，**未实机验证**）。

## 5. 响应结构（假设，待实机验证）

Web API JSON 的通用包裹形态（公开项目一致）：顶层 `statusCode`（0/成功）或
`code`，数据体常见 `data.*` / `itemList` / `aweme_list`。单个视频条目按
TikTok web 惯例假设：

```
{
  "id": "7xxxxxxxxxxxx",                  # video_id
  "create_time": 1717000000,              # publish_time (Unix 秒)
  "author": {"id": "6xxxxxxxxxxxx"},      # author_id
  "video": {"duration": 15300, ...},      # 毫秒
  "statistics": {"play_count": .., "digg_count": .., "comment_count": .., "share_count": ..},
  "commerce_info" / "ad_info" / "is_ad_fake": ...   # is_paid(Spark Ads) 候选字段
}
```

`is_paid`（Spark Ads 投流标识）在公开数据结构中**无唯一权威字段**，
`video_selectors.json` 里按候选路径列表实现：任一命中为真即 `True`，
全部缺失为 `False`——**假设，待实机验证**。

## 6. 待实机验证清单（Observe→Capture→Rebuild 步骤）

1. 打开 TikTok Shop 商品页，`list_network_requests` 过滤 `/api/`，确认商品
   视频列表真实端点与 query 全参数（尤其 `msToken`、`X-Bogus`、翻页 `cursor`）。
2. `break_on_xhr` 于 X-Bogus 附加点，确认签名函数所在脚本（webmssdk）与
   magic 常量现值，与 `signer.py` 的 `DEFAULT_CONFIG` 比对。
3. 用固定 UA + 固定 query + 固定时间戳对拍：本地 `TikTokSigner` 产出 vs
   页面 `generateX-Bogus` 产出，逐字节定位 first divergence。
4. 验证响应包裹层与 `video_selectors.json` 的 `root_candidates`/字段路径。
5. 校准 `is_paid` 真实字段（找一条明确投流 Spark Ads 视频对拍）。

## 7. 实现落点

| 文件 | 内容 |
|---|---|
| `src/scrapers/signer.py` | `TikTokSigner`：纯 Python X-Bogus（§4），常量可配置（`update_config`/构造注入），`sign_func` 可整体替换签名函数 |
| `src/scrapers/parsers/video_parser.py` | `VideoParser`：API JSON / 商品页内嵌 JSON → VIDEO_FIELDS |
| `src/scrapers/video_selectors.json` | 端点、root 候选、字段路径（全部 `verified: false`） |
| `src/scrapers/video_scraper.py` | `VideoStreamScraper(BaseScraper)`：top10 采样、8-12s 随机间隔（env）、日配额 500 |
