# TK 爬虫开发 GitHub 参考仓库清单

> 查询日期：2026-10-07（GitHub 实时搜索，star 数与维护状态为当日数据）
> 用途：支撑 TK 选品爬虫 + TKCralw 达人发现爬虫的技术选型。
> 纪律：未逐个验证代码可用性，引入前须各跑一次 smoke test；star 数会变动，以链接为准。

---

## 一、内容/视频数据采集（选品爬虫 P0 直接可用）

| 仓库 | Stars | 语言 | 核心价值 | 局限/风险 |
|------|-------|------|---------|-----------|
| [Evil0ctal/Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API) | 20.5k | Python | 抖音+TK 双端采集；异步 REST API、**自愈身份池**（多账号轮换）、PostgreSQL 归档、Docker 一键部署；维护极活跃（2026-10-07 仍有更新） | 覆盖内容/评论/主页，**无 Shop 商品数据**；身份池与本项目"单会话红线"冲突，只借鉴架构不照搬策略 |
| [davidteather/TikTok-Api](https://github.com/davidteather/TikTok-Api) | 6.7k | Python | 最经典的非官方 API wrapper：用户/趋势/标签 feed、签名处理、翻页 | 商店接口覆盖弱；风控对抗需自行跟进 |
| [drawrowfly/tiktok-scraper](https://github.com/drawrowfly/tiktok-scraper) | 5.2k | TypeScript | 用户/趋势/hashtag/music feed 元数据采集、URL 签名实现 | 更新频率下降；只读代码学签名逻辑，不建议直接装 |
| [Russell-Newton/TikTokPy](https://github.com/Russell-Newton/TikTokPy) | 238 | Python | Playwright 驱动、**无需登录/密钥**提取数据 | 适合做无登录态的轻量探测，吞吐低 |
| [bellingcat/tiktok-hashtag-analysis](https://github.com/bellingcat/tiktok-hashtag-analysis) | 377 | Python | 标签采集+分析 CLI，正对选品 schema 的话题维度（P1） | 研究向，吞吐低 |

## 二、TikTok Shop 生态（多为 SDK/Bot，非前台爬虫）

| 仓库 | Stars | 语言 | 核心价值 | 局限/风险 |
|------|-------|------|---------|-----------|
| [EcomPHP/tiktokshop-php](https://github.com/EcomPHP/tiktokshop-php) | 214 | PHP | 官方 Shop OpenAPI 客户端（202309+），可参考 API 结构与端点命名 | 需商家资质调用；仅作 P2 自有店铺数据回流的参考 |
| [hsib19/tiktok-shop-sdk](https://github.com/hsib19/tiktok-shop-sdk) | 20 | TypeScript | 非官方 Shop OpenAPI SDK，类型定义可参考 | 星少，仅作字段参考 |
| [M4tth1/Tiktok-Shop-Playwright-Crawl](https://github.com/M4tth1/Tiktok-Shop-Playwright-Crawl) | 18 | Python | 稀有的 Shop 前台 Playwright 爬取实现，可借鉴**商品页解析逻辑** | 星少且旧（2023），代码当参考不当依赖 |
| [Zeeshanahmad4/TikTok-Shop-Affiliate-Outreach-Bot](https://github.com/Zeeshanahmad4/TikTok-Shop-Affiliate-Outreach-Bot) | 72 | - | Seller Center 达人邀约自动化（Chrome 扩展），达人建联阶段可参考 | 与采集无关，属后期建联工具 |

**结论**：Shop 前台爬虫没有现成轮子——商品页解析必须按自有 schema（`tk-selection-data-schema.md`）自研，这条没有捷径。

## 三、反检测基建（风控对抗，复用/升级现有栈）

| 仓库 | Stars | 语言 | 核心价值 | 用法建议 |
|------|-------|------|---------|---------|
| [lexiforest/curl_cffi](https://github.com/lexiforest/curl_cffi) | 6.7k | Python | TLS/JA3/JA4/HTTP2 指纹模拟（chrome131），**现有爬虫栈主通道** | 持续跟进新版 chrome 指纹 |
| [daijro/camoufox](https://github.com/daijro/camoufox) | 12.4k | Python | 反检测 Firefox + Playwright 一体，引擎级指纹伪装 | 登录态获取/保活的更优方案（替代裸 Playwright） |
| [feder-cr/invisible_playwright](https://github.com/feder-cr/invisible_playwright) | 3.0k | Python | 同 Playwright API 的隐形版本，绕 PerimeterX/DataDome/Cloudflare 类检测；2026-05 新项目，更新极活跃 | curl_cffi 被 WAF 升级拦截后的第二通道 |
| [ProxyShard/ShardBrowser](https://github.com/ProxyShard/ShardBrowser) | 1.2k | TypeScript | 多账号指纹隔离（WebGL/WebGPU/TLS，220+ 设备档案），SOCKS5 下稳定 QUIC+WebRTC | 将来若扩身份池再启用；现阶段单会话红线用不上 |

## 四、参考实现（只读代码，不作依赖）

| 仓库 | Stars | 说明 |
|------|-------|------|
| [zerodytrash/TikTok-Live-Connector](https://github.com/zerodytrash/TikTok-Live-Connector) | 2.2k | TikTok LIVE WebSocket 协议实现（Node.js），直播间数据如需采集可参考 |
| [isaackogan/TikTokLive](https://github.com/isaackogan/TikTokLive) | 1.6k | 同上的 Python 版，维护活跃 |
| [szdc/tiktok-api](https://github.com/szdc/tiktok-api) | 1.3k | 早期非官方 API wrapper（已归档），仅作协议演进历史参考 |

## 五、采纳路径（决策已定）

1. **主体参考**：Evil0ctal/Douyin_TikTok_Download_API 的**身份池 + 归档架构**（自托管形态与本项目最接近）；但其池化策略违反本项目"单会话、不建池"红线，仅借鉴工程结构。
2. **风控分层**：curl_cffi（chrome131）跑商品页/搜索主通道 → 被 WAF 升级拦截时用 camoufox / invisible_playwright 获取和维护登录态。
3. **不引入重依赖**：drawrowfly / davidteather / szdc 等只读源码学签名与接口形态，不直接 pip install——TK 接口月级变动，解析层必须自持。
4. **Shop 前台**：无现成方案，商品页解析按 `tk-selection-data-schema.md` 的 15 字段自研；`M4tth1/Tiktok-Shop-Playwright-Crawl` 仅作页面结构参考。
5. **官方 API（P2）**：自有店铺数据回流时参考 EcomPHP/tiktokshop-php 的端点定义。
