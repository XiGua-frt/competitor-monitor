# Scout 达人搜索服务 · 交付与集成说明

> 一句话定位：一个**部署在接收方自己机器上**的 HTTP 服务，把 TikTok 达人搜索
> （关键词 → 解析 → 去重聚合 → 可解释评分 → 持久化）封装成两个业务接口。
> 接收方在自己的项目里通过 HTTP 调用，数据落在他自己的 SQLite 里。

---

## 0. 必读：授权与边界（部署前确认）

1. **数据源是部署者本人的浏览器登录会话**，不是公共服务：
   - 部署者必须自己在 Chrome 登录过 tiktok.com，并运行会话提取工具（见 §2）；
   - `.secrets/` 目录是部署者私有材料，**不随代码分发、不进任何仓库**；
   - 谁部署谁用自己的会话和自己的出口 IP——不要共享会话、不要共享代理。
2. **仅限内部低频调用**：默认只监听 `127.0.0.1`，单进程串行处理。
   不要放到公网/反代后面，不要并发压测。
3. **风控红线（内置，不可配置）**：遇到 WAF 挑战/认证失败/限流一律立即停止，
   不重试、不轮换 Cookie/IP/设备。出口必须是部署者本机的干净代理。
4. 平台服务条款与数据合规由部署者自负：本服务输出字段级溯源
   （`source / source_scope / collected_at / 不可逆 session_id_hash`），可审计。

---

## 1. 安装

要求：macOS + Google Chrome（会话提取工具目前仅支持 macOS）；Python ≥ 3.10。

```bash
cd Scout
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## 2. 提取会话（部署者本人操作，一次性）

在 Chrome 里登录 tiktok.com 后运行：

```bash
python tools/extract_tiktok_session.py
```

- 首次会弹钥匙串授权（"Chrome Safe Storage"），点"允许"；
- 产物 `.secrets/tiktok_session.json`（自动 chmod 600）；
- 会话过期（接口返回 `SESSION_REAUTH_REQUIRED`）时重新运行一次即可。

## 3. 配置与启动

```bash
export SCOUT_API_KEY="换成你自己的随机长字符串"
# 可选：
# export SCOUT_PORT=8787            # 默认 8787
# export SCOUT_HOST=127.0.0.1       # 默认回环，勿改公网
# export SCOUT_DB=./scout.sqlite3   # 业务数据落盘位置
# export SCOUT_PROXY=http://127.0.0.1:7892   # 出口代理（默认本机 7892）
# export TIKTOK_SESSION_FILE=...    # 会话文件非默认位置时

python -m app.api
```

启动成功会打印监听地址与路由表；启动失败的两种常见原因：
`API Key 未配置`（未设 `SCOUT_API_KEY`）和 `找不到会话文件`（未做 §2）。

## 4. 接口一览

所有请求都要带 `Authorization: Bearer <SCOUT_API_KEY>`。

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/v1/tiktok/creator-search` | 关键词搜索达人（同步，返回聚合评分结果） |
| POST | `/v1/tiktok/profiles/resolve` | 按 username 本地补全已落库的资料（**不发网络请求**） |
| GET | `/internal/v1/tiktok/sessions/{id}/status` | 会话状态 |
| POST | `/internal/v1/tiktok/sessions/{id}/revoke` | 撤销会话（撤销后拒绝一切新抓取） |
| POST | `/v1/tiktok/search-jobs` | **不存在**（故意 404，Phase 1 无异步任务） |

### 4.1 搜索达人

```bash
curl -X POST http://127.0.0.1:8787/v1/tiktok/creator-search \
  -H 'Authorization: Bearer <KEY>' -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: my-batch-001' \
  -d '{"keywords": ["fitness coach"], "limit": 20}'
```

请求体字段：`keywords`（必填，数组）、`hashtags`、`exclude_keywords`、
`region_codes`、`min_followers` / `max_followers` / `min_views`、
`limit`（默认/上限见 schemas）、可选 `idempotency_key`（同键重放返回首次结果）。

响应固定信封：`schema_version="1.0"` + `request_id` + `data[]` +
`pagination` + `meta`。每个达人行包含：公开资料、聚合指标
（`matched_video_count` / `total_view_count` 等）、可解释评分
（`relevance/quality/freshness/total` + `match_reasons`，`score_version="phase1-v1"`）、
溯源（`source/source_scope/signer_version/collected_at/session_id_hash`）。

**`meta.partial` 语义**：`true` 表示本轮采集不完整（软冷却/分页停滞），
结果可用但应视为部分样本；空 `data` + `partial=true` ≠ "没有达人"，
而是"这轮没采到"，稍后重试。

### 4.2 Python 集成示例

```python
import requests

SCOUT = "http://127.0.0.1:8787"
HEADERS = {"Authorization": "Bearer <KEY>"}

def search_creators(keywords: list[str], limit: int = 20) -> dict:
    r = requests.post(f"{SCOUT}/v1/tiktok/creator-search", headers=HEADERS,
                      json={"keywords": keywords, "limit": limit}, timeout=120)
    r.raise_for_status()
    body = r.json()
    if body["meta"].get("partial"):
        # 采集不完整：可用但建议稍后重试，不要当作完整结论
        print("partial:", body["meta"].get("stops"))
    return body
```

**错误处理约定**：错误统一在 `error.code` 里，稳定可编程：

| code | HTTP | 集成方该怎么做 |
|---|---|---|
| `AUTH_REQUIRED` | 401 | 检查 Bearer Key |
| `FORBIDDEN_INPUT` | 400 | 请求体带了禁止字段（cookie/url/token 等），删掉 |
| `INVALID_QUERY` | 400 | 按错误 details 修字段 |
| `SESSION_REAUTH_REQUIRED` | 401 | 会话失效：重新提取会话（§2） |
| `SESSION_REVOKED` | 401 | 会话被撤销：走 internal revoke 后重新导入 |
| `UPSTREAM_WAF_CHALLENGE` | 502 | **停止重试**，换干净出口后人工处理 |
| `UPSTREAM_RATE_LIMITED` | 429 | 退避（分钟级），不要加并发 |
| `UPSTREAM_SCHEMA_CHANGED` | 502 | 停止调用，联系服务维护者 |
| `REQUEST_BUDGET_EXCEEDED` | 429 | 默认 60 次/分钟/Key，放慢 |
| `PROFILE_UNRESOLVED` | 404 | 本地库没有该 handle；先跑一次 creator-search |

## 5. 交付物清单与验收

接收方部署完成后，按顺序自检：

1. `pip install -r requirements.txt` 成功；
2. `python tools/extract_tiktok_session.py` 产出 `.secrets/tiktok_session.json`；
3. `python -m app.api` 启动并打印路由表；
4. 无 Key 请求任意业务路由 → 401 `AUTH_REQUIRED`；
5. `POST /v1/tiktok/search-jobs` → 404（确认异步接口未开放）；
6. 携带 Key 的第一次 `creator-search` 返回 `schema_version="1.0"` 的数据。

已知边界（如实告知）：单进程串行、无异步任务、profile 页资料来自搜索结果
内嵌字段（不访问 profile 页）、评分缺分量时按剩余权重归一化
（`weight_basis` 字段可查）。
