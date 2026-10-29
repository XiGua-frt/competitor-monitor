# TikTok 达人发现 Phase 1 MVP 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`，按任务逐项执行本计划。所有步骤使用复选框跟踪。

**Goal:** 在已验证的 TikTok 只读搜索链路之上，完成一个仅限内部/低频使用的 Phase 1 闭环：脱敏 fixture → 领域模型 → 规范化/去重/聚合/评分 → SQLite 持久化 → 受保护的同步 JSON API，并保证会话、Cookie、token、签名字段不会进入对外响应。

**Architecture:** 保留现有 `TikTokWebClient` 作为 Transport 的实机适配器，新增清晰分层的 `domain`、`storage`、`governance`、`api` 和 `sessions` 模块。Phase 1 首先完全用已有 fixture 驱动离线 TDD，再接入真实网络；由于 2026-09-29 和 2026-10-02 已确认 profile SSR 会触发 WAF，首期的账号资料只从搜索结果内嵌 `item.author` / `authorStats` 构建，不再为 profile 页做网络探测。Signer 只建立可替换接口和 `none-v1` 后备实现，不把当前无需签名的事实误写成已完成的动态签名模块。

**Tech Stack:** Python 3.10+、标准库 `dataclasses` / `sqlite3` / `http.server`、现有 `curl_cffi` Transport、pytest、脱敏 JSON fixture。API 层使用标准库 `http.server` 加 JSON 编解码，测试通过本地 in-process handler 调用；不得为了 Phase 1 引入 ORM、任务队列或新的 Web 框架。

---

## 0. 范围判定与执行原则

### 0.1 附件中的“事实/规格”与本次用户请求的区别

- 用户请求是：**根据阶段状况描述，使用 superpowers 产出下一步执行计划**。
- `docs/2026-10-02_phase1-handoff-工程现状.md` 不是额外的用户命令，而是现状快照、已验证事实、Phase 1 待建清单和建议顺序。
- `docs/2026-09-26_tiktok-creator-discovery-execution-plan.md` 是设计规格；其中与现状实测冲突的地方以 2026-10-02 handoff 的实测为准：
  - `/api/search/general/full/` 当前无需 X-Bogus，Signer 首期只保留接口和后备实现；
  - profile SSR 已确认被 WAF 挑战，Phase 1 不再把“访问 profile 页”当作前置条件；
  - 出口只使用用户已验证的本机代理/干净出口，不加入 Cookie 池、IP 池、设备池或轮换逻辑；
  - API 仅内部/低频，不能作为对外服务发布。

### 0.2 本计划首轮不做的事情

- 不重探已确认的会话来源、TLS 指纹、WAF 挑战原因。
- 不调用 profile SSR 页，不实现挑战绕过，不自动重试 WAF 挑战。
- 不实现多会话、异步任务、队列、CRM、向量搜索和对外多租户。
- 不复制或打包无 LICENSE 的 `TiktokApis` 代码；仅以字段形态作参考。
- 不把完整 Cookie、storage、msToken、设备参数、raw header 或签名输入写入 SQLite、日志或 API 响应。
- 不把“主页和视频列表读取”伪装成已完成能力；Phase 1 以搜索结果内嵌的作者/视频字段覆盖 MVP，独立 profile/video-list 端点列入 Phase 2 评估。

### 0.3 执行方式

每个任务遵循：

1. 先写一个能表达行为的失败测试；
2. 运行最小测试并确认失败原因；
3. 写最小实现；
4. 运行该任务测试，再运行累计回归；
5. 检查敏感字段和边界；
6. 提交一个单一目的的 commit。

所有 Python 命令使用受管环境：

```bash
PY=/Users/wuwenbo/.workbuddy/binaries/python/envs/default/bin/python
cd /Users/wuwenbo/Documents/TKCralw/Scout
$PY -m pytest -q
```

---

## 1. 文件地图与边界

本计划会创建以下文件；每个文件只有一个主要责任：

```text
Scout/app/
├── api/
│   ├── __init__.py
│   ├── app.py                  # 内部同步 HTTP API 入口和路由
│   ├── auth.py                 # API Key 校验、敏感输入拒绝
│   ├── schemas.py              # 外部请求/响应 DTO，和数据库模型分离
│   └── errors.py               # 稳定错误码与 HTTP 映射
├── domain/
│   ├── __init__.py
│   ├── models.py               # Creator/Video/Provenance/SearchResult
│   ├── normalization.py        # username、URL、关键词、hashtag 规范化
│   ├── parser.py               # search fixture/raw record → domain
│   ├── deduplication.py        # 作者/视频去重
│   ├── aggregation.py          # 视频→作者聚合
│   └── scoring.py              # relevance/quality/freshness/total 分数
├── governance/
│   ├── __init__.py
│   ├── provenance.py           # 来源、授权依据、采集时间和 payload hash
│   └── deletion.py             # creator/session 数据删除和缓存清理
├── sessions/
│   ├── __init__.py
│   ├── models.py               # SessionMeta，不承载明文 Cookie
│   ├── importers.py            # 读取现有 session 文件并生成元数据
│   ├── vault.py                # 内存/加密存储边界；默认不把明文写盘
│   └── revoke.py               # 撤销状态检查
├── signing/
│   ├── __init__.py
│   ├── base.py                 # Signer Protocol
│   └── versions.py             # none-v1 后备实现和版本标识
├── storage/
│   ├── __init__.py
│   ├── sqlite.py                # 连接、schema、事务和迁移版本
│   └── repositories.py          # Creator/Video/Provenance/Result/Cache/Audit 仓储
├── transport/
│   ├── __init__.py
│   ├── response_classifier.py   # 成功/冷却/WAF/认证/schema 分类
│   ├── pagination.py            # cursor/has_more/重复页检测
│   └── public_readonly.py       # 对 web_client 的受控适配层
└── discovery/
    ├── __init__.py
    └── service.py               # 搜索用例编排，不直接处理 HTTP 细节

Scout/tests/
├── conftest.py
├── test_parser.py
├── test_normalization.py
├── test_deduplication.py
├── test_aggregation.py
├── test_scoring.py
├── test_persistence.py
├── test_session_vault.py
├── test_transport_policy.py
├── test_api_contract.py
└── test_security_boundaries.py
```

现有文件的处理原则：

- `Scout/app/discovery/web_client.py`：只做小幅兼容性修正（空 body、状态分类、文档中的章节引用）；不把领域逻辑塞回去。
- `Scout/tests/fixtures/search_general_fitness_coach.json`：作为离线事实来源，不改动真实字段；注意其 `data` 同时含 `user_list` 记录和 `item` 视频记录，parser 必须显式区分。
- `Scout/requirements.txt`：补充 `curl_cffi` 和测试依赖版本；不加入数据库 ORM。
- `Scout/.gitignore`：确认 `.secrets/`、SQLite 运行库和本地 `.env` 被忽略；绝不通过测试把真实会话复制到 fixture。

---

## 2. Task 1：建立测试基线与 fixture 解析契约

**目标：** 让后续开发可以稳定地从现有 fixture 得出明确输入输出，而不是先写一套假设字段名。

**Files:**
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/tests/conftest.py`
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/tests/test_parser.py`
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/app/domain/parser.py`
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/app/domain/__init__.py`

- [ ] **Step 1: 写失败测试，锁定 fixture 的两类记录。**

```python
# tests/test_parser.py
import json
from pathlib import Path

from app.domain.parser import parse_search_response

FIXTURE = Path(__file__).parent / "fixtures/search_general_fitness_coach.json"


def test_fixture_parses_video_records_and_ignores_user_card_as_video():
    response = json.loads(FIXTURE.read_text())
    parsed = parse_search_response(
        response,
        keywords=("fitness coach",),
        session_id_hash="sha256:test-session",
        collected_at="2026-10-02T00:00:00+00:00",
    )

    assert len(parsed.videos) == 13
    assert len(parsed.creators) >= 10
    assert all(video.platform_video_id for video in parsed.videos)
    assert all(creator.platform == "tiktok" for creator in parsed.creators)


def test_parser_preserves_missing_fields_as_none_not_zero():
    response = {"status_code": 0, "data": [{"type": "item", "item": {
        "id": "v1", "desc": "fitness", "author": {"unique_id": "coach"},
        "stats": {},
    }}]}
    parsed = parse_search_response(
        response, keywords=("fitness",), session_id_hash="h",
        collected_at="2026-10-02T00:00:00+00:00",
    )

    video = parsed.videos[0]
    assert video.view_count is None
    assert video.like_count is None
    assert video.comment_count is None
```

- [ ] **Step 2: 运行最小测试，确认模块尚不存在或契约未实现。**

```bash
cd /Users/wuwenbo/Documents/TKCralw/Scout
/Users/wuwenbo/.workbuddy/binaries/python/envs/default/bin/python -m pytest tests/test_parser.py -q
```

Expected: FAIL，原因是 `app.domain.parser` 或 `parse_search_response` 尚未实现。

- [ ] **Step 3: 实现最小 parser 契约。**

实现 `parse_search_response(...)`，返回一个明确的数据对象（可先用 `ParsedSearch` dataclass），规则固定为：

```python
@dataclass(frozen=True)
class ParsedSearch:
    creators: tuple[Creator, ...]
    videos: tuple[Video, ...]
    provenances: tuple[Provenance, ...]


def parse_search_response(response, *, keywords, session_id_hash, collected_at):
    if response.get("status_code") != 0:
        raise ValueError("unsupported upstream status_code")
    for record in response.get("data", []):
        if record.get("type") != "item" or not isinstance(record.get("item"), dict):
            continue
        # 只从 item.author / item.authorStats 取作者资料；不发起 profile 请求。
        # 数值缺失保持 None；不使用 0 作为缺失默认值。
        # 每个 video/creator 生成对应 provenance，raw_payload_hash 使用 canonical JSON SHA-256。
```

- [ ] **Step 4: 运行测试并检查真实 fixture 计数。**

```bash
/Users/wuwenbo/.workbuddy/binaries/python/envs/default/bin/python -m pytest tests/test_parser.py -q
```

Expected: PASS，至少得到 13 条视频记录，且 `user_list` 卡片不被误当作视频。

- [ ] **Step 5: 提交。**

```bash
git add app/domain tests/test_parser.py tests/conftest.py
git commit -m "feat: define search fixture parsing contract"
```

---

## 3. Task 2：实现统一领域模型与规范化

**目标：** 建立 `Creator`、`Video`、`Provenance`、`SearchResult` 的稳定内部类型，并将账号 URL、username、关键词和 hashtag 统一成可去重形式。

**Files:**
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/app/domain/models.py`
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/app/domain/normalization.py`
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/tests/test_normalization.py`
- Modify: `/Users/wuwenbo/Documents/TKCralw/Scout/app/domain/parser.py`

- [ ] **Step 1: 写规范化失败测试。**

```python
from app.domain.normalization import (
    canonical_profile_url, normalize_hashtag, normalize_username,
)


def test_username_and_profile_url_have_one_canonical_form():
    assert normalize_username("@Creator_A") == "creator_a"
    assert normalize_username("https://www.tiktok.com/@Creator_A?lang=en") == "creator_a"
    assert canonical_profile_url("Creator_A") == "https://www.tiktok.com/@creator_a"


def test_hashtag_removes_hash_and_casefolds():
    assert normalize_hashtag("#Fitness") == "fitness"
    assert normalize_hashtag(" fitness ") == "fitness"
```

模型必须显式使用 `None` 表示上游缺失字段；`session_id_hash` 只能以不可逆 hash 形式出现在 provenance/审计关联和 API provenance 字段中，不能作为公开 Creator DTO 的原始会话字段；原始 Cookie/token 永不进入任何 DTO。

- [ ] **Step 2: 运行测试确认失败。**

```bash
/Users/wuwenbo/.workbuddy/binaries/python/envs/default/bin/python -m pytest tests/test_normalization.py -q
```

- [ ] **Step 3: 实现模型和规范化函数。**

最小字段集合与 Spec 对齐：

```python
@dataclass(frozen=True)
class Creator:
    id: str
    platform: str
    platform_user_id: str | None
    username: str
    display_name: str | None
    bio: str | None
    profile_url: str
    avatar_url: str | None
    region_code: str | None
    language: str | None
    is_verified: bool | None
    follower_count: int | None
    following_count: int | None
    likes_count: int | None
    video_count: int | None
    first_seen_at: str
    last_seen_at: str
    deleted_at: str | None = None

@dataclass(frozen=True)
class Video:
    id: str
    platform_video_id: str
    creator_id: str
    description: str | None
    hashtags: tuple[str, ...]
    create_time: str | None
    region_code: str | None
    view_count: int | None
    like_count: int | None
    comment_count: int | None
    share_count: int | None
    video_url: str | None
    collected_at: str
    source: str
    source_scope: str
    session_id_hash: str
```

- [ ] **Step 4: 将 parser 改为只输出模型，不泄露 raw payload。**

测试 `asdict(parsed.videos[0])` 不出现 Cookie、`msToken`、`storage`、`raw_headers`、`signature` 等字段；raw JSON 只在内存中用于计算 `raw_payload_hash`。

- [ ] **Step 5: 运行累计测试并提交。**

```bash
/Users/wuwenbo/.workbuddy/binaries/python/envs/default/bin/python -m pytest tests/test_parser.py tests/test_normalization.py -q
git add app/domain tests/test_parser.py tests/test_normalization.py
git commit -m "feat: add domain models and canonical identifiers"
```

---

## 4. Task 3：实现去重、作者聚合和可解释评分

**目标：** 将多条搜索视频收敛成“一位作者一条结果”，并输出可解释的 relevance、quality、freshness 与 total score。

**Files:**
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/app/domain/deduplication.py`
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/app/domain/aggregation.py`
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/app/domain/scoring.py`
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/tests/test_deduplication.py`
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/tests/test_aggregation.py`
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/tests/test_scoring.py`

- [ ] **Step 1: 写去重测试，明确优先级。**

```python
from app.domain.deduplication import deduplicate_creators


def test_creator_dedup_prefers_stable_platform_id_then_username():
    creators = [
        creator(platform_user_id="u1", username="coach"),
        creator(platform_user_id="u1", username="coach-renamed"),
        creator(platform_user_id=None, username="@COACH"),
    ]
    result = deduplicate_creators(creators)
    assert len(result) == 1
    assert result[0].platform_user_id == "u1"
```

- [ ] **Step 2: 写聚合测试。**

```python

def test_aggregation_returns_one_creator_with_video_metrics_and_reasons():
    aggregated = aggregate_creator_results(
        creators=[coach], videos=[video_a, video_b],
        keywords=("fitness coach",), hashtags=("fitness",),
        exclude_keywords=("招聘",), now="2026-10-02T00:00:00+00:00",
    )
    assert len(aggregated) == 1
    assert aggregated[0].matched_video_count == 2
    assert aggregated[0].matched_keywords == ("fitness coach",)
    assert aggregated[0].match_reasons
```

- [ ] **Step 3: 写评分边界测试。**

```python

def test_missing_metrics_are_not_treated_as_zero_in_score_explanation():
    scored = score_creator(aggregate_with_missing_views)
    assert scored.quality_score is None or "view_count_unavailable" in scored.match_reasons
    assert scored.total_score is not None


def test_total_score_uses_spec_weights():
    scored = score_components(relevance=80, quality=60, freshness=50)
    assert scored.total_score == 0.55 * 80 + 0.35 * 60 + 0.10 * 50
```

- [ ] **Step 4: 实现最小算法。**

实现规则：

- 作者键依次使用 `platform_user_id`、规范化 profile URL、`platform + username`；展示名不得作为唯一键。
- 关键词匹配使用 Unicode casefold；同时检查视频 description、作者 bio；排除词命中时过滤。
- hashtag 比较使用规范化值。
- `engagement_rate = (likes + comments + shares) / max(views, 1)` 仅在参与项存在时计算；如果所需字段缺失，返回 `None`/`not_available`，不把缺失吞成 0。
- `total_score = 0.55 * relevance + 0.35 * quality + 0.10 * freshness`；可解释原因至少包含命中字段、命中值和计数。
- 首期使用固定 `score_version = "phase1-v1"`，写入 SearchResult，后续变更必须升版本。

- [ ] **Step 5: 用真实 fixture 做离线验收。**

```bash
/Users/wuwenbo/.workbuddy/binaries/python/envs/default/bin/python -m pytest tests/test_deduplication.py tests/test_aggregation.py tests/test_scoring.py -q
```

Expected: 所有结果按作者唯一返回；同一作者的多条命中视频被聚合；每条结果都有匹配原因或明确的 `not_available`。

- [ ] **Step 6: 提交。**

```bash
git add app/domain tests/test_deduplication.py tests/test_aggregation.py tests/test_scoring.py
git commit -m "feat: aggregate creators and score search matches"
```

---

## 5. Task 4：建立 SQLite schema、repository、缓存和审计

**目标：** 让业务数据、来源、搜索结果、缓存和 API 审计在重启后仍可恢复，同时禁止敏感会话材料落库。

**Files:**
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/app/storage/sqlite.py`
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/app/storage/repositories.py`
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/tests/test_persistence.py`
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/app/governance/provenance.py`
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/app/governance/deletion.py`

- [ ] **Step 1: 写重启恢复和敏感字段排除测试。**

```python

def test_sqlite_round_trip_survives_reopen(tmp_path, parsed_search):
    db_path = tmp_path / "scout.sqlite3"
    repo = Repository.connect(db_path)
    repo.save_search(parsed_search, request_id="req_test")
    repo.close()

    reopened = Repository.connect(db_path)
    result = reopened.get_search("req_test")
    assert len(result.creators) >= 1
    assert result.provenance[0].source == "tiktok_web"
    assert "sessionid" not in reopened.dump_table_names_and_columns()
    assert "msToken" not in reopened.dump_table_names_and_columns()
```

- [ ] **Step 2: 写 schema，至少包含以下表。**

```sql
CREATE TABLE creator (..., platform_user_id TEXT, username TEXT NOT NULL,
  profile_url TEXT NOT NULL, deleted_at TEXT, UNIQUE(platform, username));
CREATE TABLE video (..., platform_video_id TEXT NOT NULL UNIQUE,
  creator_id TEXT NOT NULL REFERENCES creator(id));
CREATE TABLE provenance (..., raw_payload_hash TEXT NOT NULL,
  session_id_hash TEXT, signer_version TEXT NOT NULL);
CREATE TABLE search_result (..., request_id TEXT NOT NULL,
  creator_id TEXT NOT NULL, score_version TEXT NOT NULL);
CREATE TABLE request_cache (..., cache_key TEXT PRIMARY KEY,
  response_json TEXT NOT NULL, collected_at TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE session_meta (..., session_id TEXT PRIMARY KEY,
  status TEXT NOT NULL, expires_at TEXT, revoked_at TEXT);
CREATE TABLE api_audit (..., request_id TEXT PRIMARY KEY,
  operation TEXT NOT NULL, outcome TEXT NOT NULL, created_at TEXT NOT NULL);
```

真实 SQL 可以按字段完整性拆开，但必须保留外键、唯一键、创建时间和删除/撤销状态。完整 session cookie 只允许存在于受控 vault 的内存或加密实现中，不能有 `cookies_json`、`storage_state` 或 raw token 列。

- [ ] **Step 3: 实现事务型 repository。**

提供以下明确方法：

```python
Repository.connect(path)
repo.save_search(parsed_search, request_id, cache_key=None)
repo.get_search(request_id)
repo.get_cached(cache_key, now)
repo.put_cached(cache_key, response_json, collected_at, expires_at)
repo.record_audit(request_id, operation, outcome)
repo.delete_creator(creator_id)
repo.purge_session(session_id_hash)
repo.close()
```

- [ ] **Step 4: 运行 persistence 测试。**

```bash
/Users/wuwenbo/.workbuddy/binaries/python/envs/default/bin/python -m pytest tests/test_persistence.py -q
```

Expected: PASS；关闭并重新打开数据库后，Creator/Video/Provenance/SearchResult/审计仍可读取。

- [ ] **Step 5: 实现删除和撤回的级联清理测试。**

删除 Creator 时清理视频关联、provenance 和 search_result 引用；撤回 session 时删除或标记该 session 产生的缓存和来源记录，且服务层在撤回状态下拒绝发新请求。

- [ ] **Step 6: 提交。**

```bash
git add app/storage app/governance tests/test_persistence.py
git commit -m "feat: persist discovery data and audit records in sqlite"
```

---

## 6. Task 5：抽出会话边界与 Signer 后备接口

**目标：** 保留现有用户本人 Chrome 会话的授权边界，支持导入/健康/撤销/到期元数据；同时让未来端点收紧时可插入 Signer，而当前搜索不被签名实现阻塞。

**Files:**
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/app/sessions/models.py`
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/app/sessions/importers.py`
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/app/sessions/vault.py`
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/app/sessions/revoke.py`
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/app/signing/base.py`
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/app/signing/versions.py`
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/tests/test_session_vault.py`
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/tests/test_transport_policy.py`

- [ ] **Step 1: 写 session 安全测试。**

```python

def test_import_returns_metadata_and_never_exposes_cookie_values(session_file):
    session = import_session(session_file)
    public = session.public_status()
    assert public["status"] == "active"
    assert "sessionid" not in repr(public)
    assert "msToken" not in repr(public)


def test_revoked_session_blocks_transport_before_network(fake_session):
    vault = InMemorySessionVault(fake_session)
    vault.revoke(fake_session.session_id)
    with pytest.raises(SessionRevokedError):
        vault.require_active(fake_session.session_id)
```

- [ ] **Step 2: 写 Signer Protocol 和 none-v1。**

```python
class Signer(Protocol):
    version: str
    def sign_readonly(self, request: ReadonlyRequest, session: SessionContext) -> SignedRequest: ...

class NoopSigner:
    version = "none-v1"
    def sign_readonly(self, request, session):
        return SignedRequest(url=request.url, params=request.params, headers=request.headers)
```

`NoopSigner` 只能用于已验证无需 X-Bogus 的只读模板；构造函数拒绝写路径、外部传入的签名字段和任意 URL/path。

- [ ] **Step 3: 实现导入/撤销/健康检查。**

`import_session()` 复用现有 `tools/extract_tiktok_session.py` 输出格式，校验 `sessionid`/`sid_tt` 与 `msToken`，生成不可逆 `session_id_hash`；默认 vault 只保存在进程内，`session_meta` 仅记录状态、来源、过期时间和 hash。健康检查调用现有 `TikTokWebClient.health()`，遇到 WAF 只返回停止状态，不重试。

- [ ] **Step 4: 运行测试并提交。**

```bash
/Users/wuwenbo/.workbuddy/binaries/python/envs/default/bin/python -m pytest tests/test_session_vault.py tests/test_transport_policy.py -q
git add app/sessions app/signing tests/test_session_vault.py tests/test_transport_policy.py
git commit -m "feat: isolate authorized session and signer boundaries"
```

---

## 7. Task 6：完善 Transport 分类、冷却和分页停止策略

**目标：** 把已实测的 WAF、空 body、状态码和分页行为变成可测试的策略，而不是让 discovery service 自己猜。

**Files:**
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/app/transport/response_classifier.py`
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/app/transport/pagination.py`
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/app/transport/public_readonly.py`
- Modify: `/Users/wuwenbo/Documents/TKCralw/Scout/app/discovery/web_client.py`
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/tests/test_transport_policy.py`

- [ ] **Step 1: 写分类测试。**

```python

def test_empty_body_is_cooling_not_success():
    assert classify_response(status=200, body="").kind == "cooling"


def test_slardar_body_is_waf_and_never_retryable():
    result = classify_response(status=200, body="<html>slardar challenge</html>")
    assert result.kind == "waf_challenge"
    assert result.retryable is False


def test_401_403_429_and_5xx_have_stable_kinds():
    assert classify_response(401, "").kind == "auth_failed"
    assert classify_response(403, "").kind == "forbidden"
    assert classify_response(429, "").kind == "rate_limited"
    assert classify_response(503, "").kind == "upstream_unavailable"
```

- [ ] **Step 2: 写分页重复和软冷却测试。**

分页器必须：

- 以 `cursor`/`has_more` 推进；
- 发现相同 cursor 或重复 page fingerprint 时停止并标记 `pagination_stalled`；
- 单关键词单搜；
- 空 body 进入指数退避/停止状态，不在当前调用内无限重试；
- WAF challenge 立即抛出 `WafChallengeError`。

- [ ] **Step 3: 将 `web_client.py` 接入分类器。**

不改变现有成功请求条件（`curl_cffi`、`chrome131`、本机代理、3-6 秒 jitter），只补：

- 空 body 在 `r.json()` 前分类；
- WAF、认证失败、软冷却和 schema 变化使用稳定异常；
- 文档将过时的 `§4.4` 改为当前 Spec 对应的实测章节 `§20`；
- 不增加挑战绕过或 Cookie/IP 轮换。

- [ ] **Step 4: 运行测试并提交。**

```bash
/Users/wuwenbo/.workbuddy/binaries/python/envs/default/bin/python -m pytest tests/test_transport_policy.py -q
/Users/wuwenbo/.workbuddy/binaries/python/envs/default/bin/python -m pytest -q

git add app/discovery/web_client.py app/transport tests/test_transport_policy.py
git commit -m "feat: enforce readonly transport stop policies"
```

---

## 8. Task 7：实现 discovery service 和离线同步搜索用例

**目标：** 把请求、解析、聚合、评分、缓存、审计组合成一个可被 API 调用的服务；先用 fake transport 验证，不触发真实网络。

**Files:**
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/app/discovery/service.py`
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/tests/test_discovery_service.py`

- [ ] **Step 1: 写 fake transport 测试。**

```python

def test_search_service_runs_search_to_creator_results_without_network(fake_transport, repo):
    service = DiscoveryService(transport=fake_transport, repository=repo)
    response = service.search(SearchQuery(
        keywords=("fitness coach",), hashtags=("fitness",),
        exclude_keywords=("招聘",), limit=20, cursor=None,
    ))
    assert response.schema_version == "1.0"
    assert response.data
    assert all(row.creator_id for row in response.data)
    assert all("sessionid" not in row.to_public_dict() for row in response.data)
    assert all("session_id_hash" in row.provenance for row in response.data)
```

- [ ] **Step 2: 实现服务接口。**

```python
@dataclass(frozen=True)
class SearchQuery:
    keywords: tuple[str, ...]
    hashtags: tuple[str, ...] = ()
    exclude_keywords: tuple[str, ...] = ()
    region_codes: tuple[str, ...] = ()
    min_followers: int | None = None
    max_followers: int | None = None
    min_views: int | None = None
    min_relevance_score: float | None = None
    limit: int = 20
    cursor: str | None = None


class DiscoveryService:
    def search(self, query: SearchQuery, *, request_id: str) -> SearchResponse:
        # 1. 规范化 query 并生成 cache_key
        # 2. 检查已撤销 session 和缓存
        # 3. transport.search(query.to_transport_request())
        # 4. parser → deduplicate → aggregate → score
        # 5. repository.save_search + record_audit
        # 6. 映射到 API DTO，明确排除 raw payload/session data
        # 7. 返回稳定分页和 partial/meta
```

`SearchQuery` 在 `app/discovery/service.py` 中定义并由 API schema 映射；fake transport fixture 在 `tests/conftest.py` 中从现有 JSON fixture 加载，不执行任何网络请求。

服务层必须将 `source`、`source_scope`、`signer_version`、`collected_at` 映射到 provenance；对当前 `none-v1` 输出明确说明是未使用动态签名的已验证只读请求模板，而不虚构签名结果。

- [ ] **Step 3: 写缓存命中和部分结果测试。**

同一规范化 query + session scope + signer version + schema version 在缓存窗口内不再次调用 transport；WAF/软冷却后的部分结果必须带 `partial=true` 与稳定错误/停止原因，不能把空结果误报成“无达人”。

- [ ] **Step 4: 运行测试并提交。**

```bash
/Users/wuwenbo/.workbuddy/binaries/python/envs/default/bin/python -m pytest tests/test_discovery_service.py -q
git add app/discovery/service.py tests/test_discovery_service.py
git commit -m "feat: add offline discovery search service"
```

---

## 9. Task 8：实现内部同步 API、鉴权和敏感输入边界

**目标：** 提供 Phase 1 的最小同步 JSON API，但只允许内部/低频调用，不开放任意上游请求能力。

**Files:**
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/app/api/__init__.py`
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/app/api/app.py`
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/app/api/auth.py`
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/app/api/schemas.py`
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/app/api/errors.py`
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/tests/test_api_contract.py`
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/tests/test_security_boundaries.py`

- [ ] **Step 1: 写 API 合约测试。**

请求路由固定为：

```text
POST /v1/tiktok/creator-search
POST /v1/tiktok/profiles/resolve
GET  /internal/v1/tiktok/sessions/{session_id}/status
POST /internal/v1/tiktok/sessions/{session_id}/revoke
```

首期 `profiles/resolve` 只解析已知 username/profile URL，并从本地已有作者资料/搜索结果补全；不得触发 profile SSR 网络请求。异步 search-jobs 路由不存在，调用应返回 404。

```python

def test_search_response_has_stable_schema_and_no_session_material(api_client):
    response = api_client.post(
        "/v1/tiktok/creator-search",
        headers={"Authorization": "Bearer internal-test-key"},
        json={"keywords": ["fitness coach"], "page": {"limit": 20}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == "1.0"
    assert "request_id" in body
    serialized = response.text
    for forbidden in ("sessionid", "msToken", "storage", "raw_headers", "signature"):
        assert forbidden not in serialized


def test_external_request_cannot_supply_cookie_or_arbitrary_url(api_client):
    response = api_client.post(
        "/v1/tiktok/creator-search",
        headers={"Authorization": "Bearer internal-test-key"},
        json={"keywords": ["fitness"], "cookie": "secret", "arbitrary_url": "https://example.com"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "FORBIDDEN_INPUT"
```

- [ ] **Step 2: 实现 API DTO 与错误码。**

稳定错误码至少包含：

```text
AUTH_REQUIRED
FORBIDDEN_INPUT
INVALID_QUERY
SESSION_REAUTH_REQUIRED
SESSION_REVOKED
UPSTREAM_WAF_CHALLENGE
UPSTREAM_RATE_LIMITED
UPSTREAM_SCHEMA_CHANGED
REQUEST_BUDGET_EXCEEDED
```

响应必须包含 `request_id`、`schema_version`、`data`、`pagination`、`meta`；每个 creator 行只包含公开 Creator、聚合 metrics、scores、match_reasons、provenance 公共字段。

- [ ] **Step 3: 实现 API Key、预算和审计。**

API Key 从环境变量或内部配置读取，使用常量时间比较；不把 key 写日志。每次调用先校验 key、请求大小、关键词数量、limit 上限和 idempotency key，再消费预算、调用 service、记录 outcome。预算只能限制误用和成本，不允许通过多会话/代理轮换提高抓取量。

- [ ] **Step 4: 运行 API 和安全测试。**

```bash
/Users/wuwenbo/.workbuddy/binaries/python/envs/default/bin/python -m pytest tests/test_api_contract.py tests/test_security_boundaries.py -q
```

Expected: 合约、鉴权、禁止字段、404 异步接口和撤回后的停止行为全部通过。

- [ ] **Step 5: 提交。**

```bash
git add app/api tests/test_api_contract.py tests/test_security_boundaries.py
git commit -m "feat: expose guarded synchronous creator search API"
```

---

## 10. Task 9：真实 Transport 小规模验收与风险护栏

**目标：** 只有离线闭环和安全测试全绿后，才用一次受控真实会话验证端到端；不以高频压力测试验证。

**Files:**
- Modify: `/Users/wuwenbo/Documents/TKCralw/Scout/tests/test_api_contract.py`
- Create: `/Users/wuwenbo/Documents/TKCralw/Scout/tests/test_live_smoke.py`
- Modify: `/Users/wuwenbo/Documents/TKCralw/docs/2026-10-02_phase1-handoff-工程现状.md`（仅记录新证据，不改写既有事实）

- [ ] **Step 1: 增加显式 opt-in 的 live smoke 测试。**

```python
@pytest.mark.skipif(
    os.getenv("SCOUT_LIVE_SMOKE") != "1",
    reason="live TikTok smoke test requires explicit opt-in",
)
def test_live_single_keyword_search():
    client = TikTokWebClient()
    rows = client.search(SearchRequest(keywords=("fitness coach",), max_results=5))
    assert rows
    assert all(row["source"] == "tiktok_web" for row in rows)
```

- [ ] **Step 2: 按运行红线执行，不并发、不循环压测。**

```bash
SCOUT_LIVE_SMOKE=1 \
/Users/wuwenbo/.workbuddy/binaries/python/envs/default/bin/python -m pytest tests/test_live_smoke.py -q
```

运行前确认：会话仍是用户本人授权、代理 `127.0.0.1:7892` 可用、当前无软冷却；运行中只查询一个关键词、最多 5 条、遵守 3-6 秒 jitter；WAF、空 body、登录失效出现时立即停止，不换池、不绕过。

- [ ] **Step 3: 验证 API 输出和数据库内容。**

检查：

- API 返回只有 `source/source_scope/signer_version/collected_at` 等 provenance 公共字段；
- SQLite 没有明文 Cookie、token、storage、raw headers 或签名输入；
- 同一作者只出现一次；
- `partial`、错误码和停止原因没有被吞掉；
- 重新启动服务后缓存和审计可读。

- [ ] **Step 4: 提交验收证据。**

```bash
/Users/wuwenbo/.workbuddy/binaries/python/envs/default/bin/python -m pytest -q
git add tests/test_live_smoke.py docs/2026-10-02_phase1-handoff-工程现状.md
git commit -m "test: record guarded phase1 live smoke evidence"
```

---

## 11. Task 10：Phase 1 DoD 审查与发布闸门

**目标：** 用可复现证据判断 Phase 1 是否完成，而不是以“代码能启动”作为完成标准。

- [ ] **Step 1: 运行全量离线测试。**

```bash
/Users/wuwenbo/.workbuddy/binaries/python/envs/default/bin/python -m pytest -q
```

Expected: 所有 session、transport、domain、persistence、API contract、security boundary 测试通过。

- [ ] **Step 2: 对照 DoD 逐项打勾。**

必须有证据的项目：

- 使用用户明确授权的浏览器会话；
- 只读请求模板已登记；
- Signer 接口存在，当前 `none-v1` 与已验证无签名端点事实一致；动态签名实现明确列为后备能力，不得声称已完成；
- 外部请求不能传 Cookie/storage/设备参数/签名字段；
- 不支持写接口、Cookie 池、IP 池、设备池和代理轮换；
- Creator/Video/Provenance/SearchResult 已落地；
- SQLite 保存业务数据、缓存、审计且可重启恢复；
- 关键词、hashtag、排除词、地区和粉丝区间过滤可测试；
- relevance/quality/freshness 分及命中原因可解释；
- 稳定 ID/profile URL/username 去重；
- 响应包含来源、范围、不可逆 `session_id_hash`、signer version、采集时间；原始 Cookie/token/storage 永不返回；
- schema 版本稳定；
- 401/403/WAF/429/schema 变化安全停止；
- 撤回会话后不能发新请求；
- API、领域、安全和持久化测试通过；
- `fitness coach`、中英文混合词、歧义词三类业务验收已记录，且结果能够解释。

- [ ] **Step 3: 记录未满足项而不是强行宣布完成。**

如果动态 Signer、独立 profile/video-list 读取或真实业务 Top 20 人工相关率尚未完成，状态写为“Phase 1 MVP 可用，Phase 1 完整 DoD 未闭合”，并将其转入 Phase 2 闸门；不得通过修改 DoD 文本来掩盖缺口。

- [ ] **Step 4: 提交最终审查记录。**

```bash
git log --oneline -n 12
git status --short
/Users/wuwenbo/.workbuddy/binaries/python/envs/default/bin/python -m pytest -q
```

---

## 12. 推荐执行批次与停止条件

### 批次 A：先完成离线领域闭环

执行 Task 1-4。完成条件：fixture 能解析、作者能去重聚合、评分可解释、SQLite 重启恢复。此批次完全不访问 TikTok 网络。

### 批次 B：完成授权边界和 Transport 策略

执行 Task 5-6。完成条件：session revoke 阻断请求、Signer 接口可替换、WAF/空 body/429/分页重复均有确定行为。

### 批次 C：接入服务和 API

执行 Task 7-8。完成条件：fake transport 下 API 合约全绿，敏感字段输入/输出测试全绿，异步接口不误开放。

### 批次 D：一次受控实机验收

执行 Task 9-10。任何以下情况出现都停止当前批次，不通过重试绕过：

- WAF challenge；
- 空 body 软冷却；
- session 失效或权限异常；
- schema 与 fixture/解析契约不一致；
- 发现敏感材料进入日志、数据库或响应；
- 需要增加代理/IP/Cookie/设备轮换才能继续。

---

## 13. Phase 2 明确入口

Phase 1 MVP 通过后再处理：

1. 软冷却窗口实测标定和更精确退避曲线；
2. cursor/has_more/search_id 长时间分页稳定性；
3. profile 无签名可用端点的合规研究，前提是不会绕过 WAF；
4. 动态 Signer（只有目标端点确实收紧且有授权证据时）；
5. schema 版本、字段缺失率、Top 20 人工评审和刷新/过期策略；
6. 只有同步接口有真实调用方且耗时证据充分时，才评估异步任务。

本计划不把这些内容提前塞进 Phase 1，避免在已验证的搜索底座之上重新引入网络探测和风控风险。
