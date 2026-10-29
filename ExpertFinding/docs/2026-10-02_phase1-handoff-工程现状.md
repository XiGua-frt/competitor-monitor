# TikTok 达人发现 · 工程现状交接（2026-10-02）

> 用途：开新会话制定/执行 Phase 1 计划时的现状快照。所有结论均已实机验证，标注了证据来源。
> 上游文档：`docs/2026-09-26_tiktok-creator-discovery-execution-plan.md`（v2 Spec，1353 行，本文引用其章节号）。

## 1. 一句话现状

**数据源已通、Spec 已定稿、代码只有最底层一层**。Phase 0 的核心问题（会话从哪来、请求能不能通）已全部解决并有实测证据；Phase 1（领域模型 + 同步 API MVP，Spec §14）尚未开工。

## 2. 已验证的事实（不用再探，直接采信）

| # | 事实 | 证据 |
|---|---|---|
| 1 | 数据源 = 自建爬虫，TikHub 已否决 | 用户 2026-09-26 拍板 |
| 2 | `/api/search/general/full/` **无需 X-Bogus 签名**，返回 status_code=0 真实数据（13-17 条/页，has_more 可翻页） | 2026-09-29 实测 + **2026-10-02 复测仍通**（17 条，含 720w 粉作者） |
| 3 | 成功三要素：①真实浏览器会话 cookie ②curl_cffi `impersonate="chrome131"` TLS 指纹 ③干净出口（本机系统代理 127.0.0.1:7892） | 2026-09-29 实测；httpx/curl 裸指纹被 Slardar WAF 拦（1462 字节挑战壳） |
| 4 | 会话来自用户本人 Chrome（v153，Default profile），35 条 cookie，sessionid 有效至 2027-01，msToken 至 2026-12 | `Scout/.secrets/tiktok_session.json`（chmod 600，不入 Git） |
| 5 | profile SSR 页（`/@user`）**带会话也被 WAF JS 挑战拦** → 作者资料从搜索结果内嵌 `item.author` / `authorStats` 取（粉丝/获赞/视频数/bio 齐全，够评分） | 2026-09-29 实测 |
| 6 | 搜索 API 有 **IP 级软冷却**：密集请求后返回 200 空 body（非 429），冷却数分钟量级 | 2026-09-29 实测 |
| 7 | Chrome 130+ cookie 解密：v10 + AES-128-CBC + PBKDF2(saltysalt,1003,16B)，**meta.version>=24 时明文前 32 字节是 SHA256(domain) 必须去掉** | 提取工具已实现并跑通 |
| 8 | TiktokApis 参考仓库**无 LICENSE** → 生产代码必须自研，只作字段/请求形态参考 | 计划 §19、§3.2 |
| 9 | 签名模块（Spec §5）首期**降级为后备方案**，Phase 1 不阻塞；但若端点收紧需按 §5 补 | §20.2 |

## 3. 现有代码与资产

```text
Scout/
├── app/discovery/web_client.py        ← 已验证客户端（唯一的新代码，139 行）
├── app/scrapers/                      ← Scout 旧爬虫（tiktok.py SSR 方式已失效，其他平台未动）
├── tools/extract_tiktok_session.py    ← 会话一键提取（Chrome→解密→.secrets/）
├── .secrets/tiktok_session.json       ← 会话材料（600 权限，绝不入 Git）
├── tests/fixtures/search_general_fitness_coach.json   ← 真实响应 fixture（已脱敏校验，355KB）
└── requirements.txt                   ← 旧 Scout 依赖；新链路额外依赖 curl_cffi（含 h2）

docs/2026-09-26_tiktok-creator-discovery-execution-plan.md   ← v2 Spec（§1-20）
docs/2026-10-02_phase1-handoff-工程现状.md                    ← 本文档
```

**web_client.py 已实现**（`Scout/app/discovery/web_client.py`）：
- `TikTokWebClient`：`load_session()` / `search(SearchRequest)`（翻页+多关键词）/ `health()` / `quota()`
- 内置：3-6s 随机抖动限速、WAF 挑战页检测即抛 `WafChallengeError`（不重试不绕过）、每条记录附 `source/source_scope/authorization_basis/collected_at` 溯源字段
- 代理优先级：`SCOUT_PROXY` 环境变量 > 127.0.0.1:7892 > 无

## 4. Phase 1 待建清单（Spec §14 Phase 1，按 §13 代码结构）

现状差距：Spec §13 规划的分层（Session / Signing / Transport / Discovery / Domain / API）目前只有 Transport 底座（web_client.py）。待建：

1. **统一领域模型**（Spec §10）：Creator / Video / Provenance / SearchResult —— fixture 里的 raw JSON → 模型映射
2. **SQLite schema + repository**（Spec §12）：含缓存与审计表，重启不丢
3. **会话管理层**（Spec §4）：从 web_client 内嵌逻辑抽出——导入/加密/健康检查/撤销/到期提醒
4. **账号规范化 + 去重 + 作者聚合**（Spec §11.1-11.3）：同一作者不重复返回
5. **过滤与评分**（Spec §11.4）：关键词/Hashtag/排除词/地区/粉丝过滤 + 评分与命中原因
6. **同步 API**（Spec §7）：搜索 / 账号补全 / 详情 + API Key 鉴权 + 请求预算 + 审计
7. **测试**（Spec §15）：用已有 fixture 做离线单元/合约测试；对外响应不得含 cookie/token 等敏感字段
8. Signer 接口留空位（§5 后备），Phase 1 可不做实现

## 5. 环境约束（新会话必须知道）

- **出口依赖**：链路只在用户 Mac 上验证过（代理 127.0.0.1:7892）。沙箱共享出口被 WAF 拦。开发/测试都在这台 Mac 上做。
- **Python 环境**：受管 venv `/Users/wuwenbo/.workbuddy/binaries/python/envs/default/bin/python`，已装 curl_cffi / httpx[http2] / cryptography。
- **频率红线**：单 IP 软冷却实测存在 → 生产逻辑必须"单关键词单搜 + 指数退避 + 空 body 视为冷却信号"。
- **会话红线**：单会话、不建 Cookie 池/账号池（Spec §2.2）；WAF 挑战 = 停止 + 人工处理，不绕过。
- **对外 API 边界**（用户已确认的决策）：API 先只做内部/低频，**不能现在对外发给别人**——会话是用户本人登录态，量级上去有封号风险，且出口绑死本机。对外需先过：专用账号、独立部署、合规评估。

## 6. 遗留待办（§20.6，不阻塞 Phase 1 开工）

- [ ] 软冷却窗口时长标定（退避曲线）
- [ ] 翻页 search_id 机制深度验证（cursor/has_more 首测正常）
- [ ] 资料补全路径：搜索内嵌 author 字段是否足够（初判够），或找可用的无签名 profile 端点
- [ ] msToken 2026-12 到期前重提取（工具已支持一键重跑）
- [ ] `web_client.py` 文档字符串里引用的 §4.4 已被 v2 重构取代，实为 §20——顺手更正

## 7. 新会话建议的开工顺序

1. 读 Spec §7 / §10-13 / §14 Phase 1（本文 §4 已列要点）
2. 用 `tests/fixtures/search_general_fitness_coach.json` 先写领域模型映射 + 去重聚合的离线测试（不碰网络）
3. 再接 web_client 做端到端（每步实机验证遵守：限速 + 空 body 退避 + WAF 即停）

---

## 8. 2026-10-02 Phase 1 执行记录（仅追加，不改写既有内容）

本节为 2026-10-02 晚些时候的执行记录，只追加新事实，上文 §1-7 未作任何修改。

### 8.1 分支与环境

- 工作分支：`feat/phase1-discovery-mvp`（在 `Scout/` 仓库内，`git branch --show-current` 可复现）。
- Python：受管 venv `/Users/wuwenbo/.workbuddy/binaries/python/envs/default/bin/python`（与 §5 一致）。

### 8.2 Task 1-8 已全部离线落地（无真实网络请求）

按 §4 清单完成的分层代码均已存在于工作区（可直接 `ls` 复核）：

- `app/domain/`：models / normalization / parser / deduplication / aggregation / scoring
- `app/transport/`：response_classifier / pagination / public_readonly（+ 既有的 `app/discovery/web_client.py`）
- `app/sessions/`：models / importers / vault / revoke
- `app/signing/`：base（Signer Protocol）/ versions（none-v1 后备）
- `app/storage/`：sqlite / repositories
- `app/governance/`：provenance / deletion
- `app/api/`：app / auth / schemas / errors
- `app/discovery/service.py`：搜索用例编排
- `tests/`：`test_parser` / `test_normalization` / `test_deduplication` / `test_aggregation` / `test_scoring` / `test_persistence` / `test_session_vault` / `test_transport_policy` / `test_api_contract` / `test_security_boundaries` / `test_discovery_service`

全部用例以 `tests/fixtures/` 离线 fixture 与合成数据驱动，没有发起真实 TikTok 请求，也没有读取 `.secrets/`。

### 8.3 全量测试结果（2026-10-02 复现命令）

```bash
cd /Users/wuwenbo/Documents/TKCralw/Scout
/Users/wuwenbo/.workbuddy/binaries/python/envs/default/bin/python \
  -m pytest -q --basetemp=$PWD/.superpowers/tmp/rt9
```

结果：**259 passed, 1 skipped**（约 1s）。唯一的 skip 即 §8.4 的 live smoke。

### 8.4 Task 9 live smoke：文件已建，尚未执行

- 新增 `tests/test_live_smoke.py`：单关键词（fitness coach）、`max_results=5`、断言 `source == "tiktok_web"` 及 provenance 契约；文件顶部 docstring 写明运行红线（不并发、不循环压测、一个关键词、最多 5 条、3-6 秒 jitter、WAF/空 body/登录失效立即停止、不换池不绕过、需代理 127.0.0.1:7892 可用）。
- 守卫：`@pytest.mark.skipif(os.getenv("SCOUT_LIVE_SMOKE") != "1", ...)`。已验证不设该环境变量时用例被 skip（`1 skipped`，SKIPPED 原因 "live TikTok smoke test requires explicit opt-in"），不构造 client、不读会话、零网络请求。
- **真实联网验收尚未执行**，待用户确认会话授权与代理可用后由编排方执行：

```bash
SCOUT_LIVE_SMOKE=1 \
/Users/wuwenbo/.workbuddy/binaries/python/envs/default/bin/python \
  -m pytest tests/test_live_smoke.py -q
```

执行后应补记：返回条数、`last_stop_reason`（None / cooling_stopped / pagination_stalled / pagination_stopped）、是否触及 WAF 或冷却，以及 API 输出与 SQLite 是否仍无明文 cookie/token（Task 9 Step 3 的五项检查）。

### 8.5 本轮未做的事

- 未执行任何 git 命令（新增文件仍为未提交状态，提交待后续单一目的 commit）。
- 未读取 `.secrets/` 下任何会话材料。
- 未做 profile SSR、未加签名实现、未加任何池化/重试绕过逻辑（与 §0.2 的执行边界一致）。
