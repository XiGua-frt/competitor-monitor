# TikTok 关键词找达人执行 Spec

> 版本：v3.0
>
> 日期：2026-09-26
>
> 状态：Ready for implementation review
>
> 项目范围：仅 TikTok；首期使用“授权浏览器会话 + 自建请求签名模块”读取公开搜索、公开主页和公开视频数据，并通过 HTTP JSON API / Python SDK 对外提供结果。
>
> 关联项目：
> - `/Users/wuwenbo/Documents/TKCralw/Scout`
> - `/Users/wuwenbo/Documents/TKCralw/TiktokApis`

## 1. 执行摘要

本项目采用自建 TikTok Web 请求适配层，但只处理**公开数据、只读请求和用户明确授权的浏览器会话**。系统不做互动、不做发布、不做私信，不通过 Cookie 池、IP 池或设备池规避平台限制。

总体链路：

```text
授权浏览器会话
        ↓
Session Context
Cookie / storage / UA / device context / header contract
        ↓
Request Signer
基于当前请求动态生成签名，不复放旧抓包签名
        ↓
TikTok Public Web Transport
搜索 / 主页 / 视频 / 用户公开数据
        ↓
Discovery Adapter
候选视频、候选作者、分页和错误分类
        ↓
Normalize / Resolve / Deduplicate
        ↓
Keyword Matching / Ranking
        ↓
SQLite / Provenance / Cache
        ↓
HTTP JSON API / Python SDK
```

### 1.1 核心技术决策

| 项目 | 决策 |
|---|---|
| 数据获取方式 | 自建 Web 请求签名 + 授权浏览器会话 |
| 数据范围 | 公开搜索、公开主页、公开视频及其公开指标 |
| 写操作 | 禁止 |
| Cookie 池 | 第一阶段不做 |
| IP 池 | 第一阶段不做 |
| 设备指纹池 | 不做 |
| 代理轮换 | 不用于规避限制 |
| 签名材料 | 从当前会话和当前请求动态生成，不复放静态抓包结果 |
| API 交付 | HTTP JSON API，可选 Python SDK |
| 存储 | 第一阶段直接使用 SQLite |
| 异步任务 | 第二阶段再做 |
| CSV | 不作为对外交付形式 |

### 1.2 安全边界

自建签名模块只允许用于：

- 用户自己控制或明确授权的浏览器会话；
- 公开页面和公开数据的只读请求；
- 合法、低频、受控的业务调用；
- 经过会话权限和用途审查的数据处理。

明确禁止：

- 伪造或批量生成第三方登录会话；
- 购买、收集、共享或轮换他人 Cookie；
- 通过签名、代理、设备参数或重试绕过验证码、WAF、封禁、配额或访问控制；
- 访问私信、通知、收藏、草稿、后台管理或其他非公开数据；
- 点赞、关注、评论、私信、发布、收藏、举报等写操作；
- 将 TiktokApis 中没有明确授权的代码或签名文件直接复制到生产项目。

## 2. 产品目标与非目标

### 2.1 首期目标

首期交付一个可以直接调用的 TK-only API：

1. 接收关键词、Hashtag、排除词、地区和时间范围；
2. 使用授权会话访问公开搜索结果；
3. 获取候选视频及其公开作者信息；
4. 聚合相同作者的命中视频；
5. 补齐公开主页资料和公开指标；
6. 按内容相关度、粉丝规模、互动表现和新鲜度排序；
7. 支持已知 TikTok 主页的资料补全；
8. 保存来源、会话来源、采集时间和字段状态；
9. 对外返回稳定的 HTTP JSON 响应；
10. 在会话过期、限流、验证码或接口变化时安全停止。

### 2.2 首期不承诺

首期不能对外承诺：

- 抓取 TikTok 全部公开数据；
- 全平台、全地区、全历史时间段覆盖；
- 实时搜索所有新发布视频；
- 任何数量级的无限并发；
- 通过轮换会话或出口提高覆盖率；
- 对搜索结果做完整性保证；
- 获取非公开信息。

系统对外应使用以下表述：

> 在授权浏览器会话可访问的 TikTok 公开 Web 数据范围内，按关键词发现并筛选创作者。

## 3. 数据采集分层

### 3.1 Session Layer

负责维护用户提供的授权浏览器会话：

```text
BrowserSession
- session_id
- owner_id
- consent_record_id
- cookie_jar_encrypted
- local_storage_encrypted
- session_storage_encrypted
- user_agent
- device_context_encrypted
- browser_metrics_encrypted
- origin
- created_at
- last_validated_at
- expires_at
- status
```

会话状态：

```text
created
active
expiring
expired
revoked
blocked
needs_reauthentication
```

### 3.2 Signing Layer

负责为当前请求生成所需的动态签名和请求头。

```text
RequestSigner
- signer_version
- required_context()
- sign(method, url, params, body, session_context)
- validate_signature_inputs()
- health()
```

约束：

- 不接受调用方直接传入签名字段；
- 不保存可复用的静态签名结果；
- 签名输入必须绑定当前 URL、参数、请求体和当前会话上下文；
- 缺少必要的浏览器上下文时直接失败，不伪造默认值；
- 签名器版本必须可追踪；
- 上游字段变化时隔离适配器，不把异常响应写入领域模型。

### 3.3 Transport Layer

负责发送只读请求，不负责业务评分。

```text
TikTokWebTransport
- request_public_search()
- request_public_profile()
- request_public_video_list()
- request_public_video_detail()
- classify_response()
```

Transport 只能调用登记过的只读请求模板。禁止把任意 URL、任意路径和任意方法暴露给外部调用方。

### 3.4 Discovery Layer

负责将 TikTok Web 结果转换为统一候选对象：

```text
PublicSearchDiscovery
- normalize_query()
- search_videos()
- search_creators()
- paginate()
- extract_candidates()
```

### 3.5 Domain Layer

负责：

- Creator / Video 统一模型；
- 账号规范化；
- 视频到作者聚合；
- 去重；
- 关键词匹配；
- 评分；
- 来源和字段状态。

## 4. 浏览器会话接入 Spec

### 4.1 首期会话来源

首期只支持以下方式之一：

1. 用户在本地受控浏览器中登录 TikTok 后，通过本地导入流程提供会话上下文；
2. 用户使用本项目提供的浏览器扩展/本地采集器，在明确提示和确认后导出当前会话上下文；
3. 企业内部受控浏览器环境通过 Secret 注入会话上下文。

不支持：

- 从第三方购买 Cookie；
- 从日志或抓包文件批量导入 Cookie；
- 从多个账号自动采集会话；
- 将一个会话共享给不相关的租户。

### 4.2 会话上下文要求

具体字段由当前会话和当前请求证据决定，不允许硬编码无证据的默认值。可能涉及：

```text
cookie
user_agent
local_storage
session_storage
msToken
browser_metrics
device_id / odin_id（仅在当前会话实际提供时）
csrf 相关字段（只读请求不主动生成写操作字段）
请求来源页
header order contract
```

敏感字段要求：

- 只在服务端加密保存；
- 不写入 API 响应；
- 不写入普通日志；
- 不进入 SQLite 明文列；
- 不进入测试 fixture；
- 不提交 Git；
- 支持主动撤销和立即删除。

### 4.3 会话绑定

首期一个 `session_id` 只绑定一个明确的会话上下文。默认不支持：

```text
同一 Cookie 跨多个 UA 使用
同一 Cookie 跨多个设备上下文使用
同一 Cookie 在多个出口之间频繁切换
```

这样做的目的不是规避平台控制，而是保持会话证据和请求上下文一致，降低数据错配、会话失效和错误请求风险。

### 4.4 会话健康检查

健康检查只使用登记过的只读公开请求或低风险会话验证请求。

输出：

```json
{
  "session_id": "sess_xxx",
  "status": "active",
  "last_validated_at": "2026-09-26T00:00:00Z",
  "expires_at": null,
  "reason": null
}
```

遇到以下情况必须标记为不可用：

- 登录重定向；
- 明确的会话过期；
- WAF/验证码挑战；
- 401/403 权限拒绝；
- 签名上下文不完整；
- 多次 schema 校验失败。

不可用会话不得自动轮换到其他会话继续请求。

## 5. 自建签名模块 Spec

### 5.1 设计目标

自建签名模块用于复现**当前授权浏览器会话的只读请求契约**，而不是用于隐藏身份、绕过限制或批量制造请求身份。

签名过程抽象为：

```text
输入：
  method
  origin
  path
  ordered query params
  request body
  referer
  user-agent
  session browser context
  signer version

处理：
  validate inputs
  build exact request contract
  calculate current request signatures
  attach generated headers/query values

输出：
  signed readonly request
```

### 5.2 签名器接口

```python
from dataclasses import dataclass
from typing import Mapping

@dataclass(frozen=True)
class SignRequest:
    method: str
    origin: str
    path: str
    query: tuple[tuple[str, str], ...]
    body: bytes | None
    referer: str
    session_id: str

@dataclass(frozen=True)
class SignedRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None
    signer_version: str
    session_id: str

class Signer:
    name: str
    version: str

    def required_context(self) -> tuple[str, ...]:
        raise NotImplementedError

    def sign(self, request: SignRequest, context: Mapping) -> SignedRequest:
        raise NotImplementedError

    def validate(self, signed_request: SignedRequest) -> None:
        raise NotImplementedError
```

### 5.3 签名器禁止事项

- 不允许调用方直接传递签名参数；
- 不允许从旧请求复制时间敏感签名；
- 不允许把完整浏览器抓包作为固定模板无限重放；
- 不允许缺少字段时自动填充伪造值；
- 不允许通过修改签名参数绕过验证码或访问控制；
- 不允许将签名器扩展到写接口；
- 不允许在日志中打印完整 URL、Cookie、token 或签名头。

### 5.4 签名器测试

必须使用脱敏的固定请求样本验证：

- 参数顺序；
- URL 编码；
- 请求体编码；
- header contract；
- 签名版本；
- 会话上下文缺失时的失败行为；
- 当前时间或随机字段的生命周期；
- 上游响应错误分类。

测试样本只能保存脱敏字段和哈希，不能保存真实 Cookie、真实 token 或完整浏览器会话。

## 6. 公开数据请求范围

### 6.1 首期允许的读取对象

- 公开搜索结果；
- 公开用户主页；
- 公开视频列表；
- 公开视频详情；
- 公开作者简介和公开指标；
- 公开 Hashtag、描述和视频互动指标（上游实际返回时）。

### 6.2 首期禁止的对象

- 私信和聊天；
- 通知；
- 收藏夹和私人列表；
- 草稿；
- Creator Studio 管理数据；
- 商业后台数据；
- 任何非公开内容；
- 任何写操作接口。

### 6.3 请求模板登记

每个请求模板必须登记：

```text
operation_name
http_method
path_pattern
allowed_query_fields
allowed_body_fields
required_context
readonly = true
expected_response_schema
pagination_mode
rate_policy
error_mapping
reviewed_at
```

示例：

```text
operation_name: public_search_v1
readonly: true
allowed_query_fields: keyword, region, cursor, count
required_context: cookie, user_agent, ms_token, browser_metrics
```

外部 API 不允许传入任意 `path`、任意 header 或任意签名字段。

## 7. 对外 API Spec

### 7.1 同步关键词搜索

首期只开放同步接口：

```http
POST /v1/tiktok/creator-search
Authorization: Bearer <api-key>
Content-Type: application/json
Idempotency-Key: <client-generated-key>
```

请求：

```json
{
  "keywords": ["fitness coach", "健身教练"],
  "hashtags": ["fitness", "workout", "健身"],
  "exclude_keywords": ["招聘"],
  "region_codes": ["US"],
  "date_range": {
    "start": "2026-08-27",
    "end": "2026-09-26"
  },
  "filters": {
    "min_followers": 10000,
    "max_followers": 500000,
    "min_views": 1000,
    "min_relevance_score": 60
  },
  "sort": {
    "by": "total_score",
    "order": "desc"
  },
  "page": {
    "limit": 20,
    "cursor": null
  }
}
```

外部请求不能包含：

```text
cookie
msToken
device_id
browser_metrics
raw_headers
signature
signer_input
arbitrary_url
arbitrary_path
```

### 7.2 搜索响应

```json
{
  "request_id": "req_01J...",
  "schema_version": "1.0",
  "data": [
    {
      "creator_id": "tiktok_creator_123",
      "platform": "tiktok",
      "username": "creator_a",
      "display_name": "Creator A",
      "profile_url": "https://www.tiktok.com/@creator_a",
      "bio": "...",
      "region_code": "US",
      "is_verified": false,
      "follower_count": 125000,
      "metrics": {
        "matched_video_count": 12,
        "total_matched_views": 4200000,
        "average_matched_views": 350000,
        "max_matched_views": 810000,
        "engagement_rate": 0.083
      },
      "scores": {
        "relevance_score": 94,
        "quality_score": 82,
        "freshness_score": 96,
        "total_score": 89.8
      },
      "match_reasons": [
        {
          "type": "video_description",
          "value": "fitness coach",
          "count": 8
        },
        {
          "type": "hashtag",
          "value": "fitness",
          "count": 10
        }
      ],
      "provenance": {
        "source": "authorized_tiktok_web_session",
        "source_scope": "public_web_readonly",
        "session_scope": "owner_authorized",
        "signer_version": "signer-v1",
        "collected_at": "2026-09-26T00:00:00Z",
        "data_freshness": "fresh"
      }
    }
  ],
  "pagination": {
    "next_cursor": "opaque_cursor",
    "has_more": true
  },
  "meta": {
    "coverage": "session_limited",
    "partial": false,
    "sources_used": ["authorized_tiktok_web_session"]
  }
}
```

### 7.3 已知账号补全

```http
POST /v1/tiktok/profiles/resolve
Authorization: Bearer <api-key>
Content-Type: application/json
```

```json
{
  "profiles": [
    "https://www.tiktok.com/@creator_a",
    "creator_b"
  ],
  "include_videos": true,
  "video_limit": 20,
  "match": {
    "keywords": ["fitness", "健身"],
    "exclude_keywords": ["招聘"]
  }
}
```

### 7.4 会话管理接口

会话管理只供内部管理员或受控租户使用，不向普通搜索调用方暴露 Cookie：

```http
POST /internal/v1/tiktok/sessions/import
POST /internal/v1/tiktok/sessions/{session_id}/validate
POST /internal/v1/tiktok/sessions/{session_id}/revoke
GET  /internal/v1/tiktok/sessions/{session_id}/status
```

导入接口接收加密传输的会话上下文，返回的只有：

```json
{
  "session_id": "sess_xxx",
  "status": "active",
  "expires_at": null
}
```

不返回 Cookie、storage、token、设备参数和签名结果。

### 7.5 异步接口

首期不开放。只有在同步 API、SQLite、请求预算和会话健康管理稳定后，再实现：

```http
POST /v1/tiktok/search-jobs
GET  /v1/tiktok/search-jobs/{job_id}
GET  /v1/tiktok/search-jobs/{job_id}/results
POST /v1/tiktok/search-jobs/{job_id}/cancel
```

## 8. 错误和停止策略

错误响应：

```json
{
  "error": {
    "code": "SESSION_REAUTH_REQUIRED",
    "message": "TikTok 浏览器会话已失效，需要重新授权",
    "request_id": "req_01J...",
    "retryable": false,
    "source": "authorized_tiktok_web_session"
  }
}
```

首期错误码：

```text
INVALID_REQUEST
UNAUTHORIZED
SESSION_NOT_FOUND
SESSION_REAUTH_REQUIRED
SESSION_CONTEXT_INCOMPLETE
SESSION_REVOKED
SIGNER_CONTEXT_INVALID
SIGNER_VERSION_UNSUPPORTED
SOURCE_SCHEMA_CHANGED
SOURCE_RATE_LIMITED
SOURCE_QUOTA_EXCEEDED
ACCESS_CHALLENGE_DETECTED
SOURCE_TEMPORARY_FAILURE
NO_RESULTS
PROFILE_NOT_FOUND
CURSOR_INVALID
INTERNAL_ERROR
```

处理规则：

| 情况 | 动作 |
|---|---|
| 401/登录重定向 | 标记会话需重新授权，停止当前请求 |
| 403/权限拒绝 | 停止当前会话请求，不切换身份 |
| 验证码/WAF Challenge | 停止当前任务，不自动绕过 |
| 429/配额限制 | 按来源规则冷却或返回错误，不换 IP 继续请求 |
| 5xx/网络抖动 | 有限指数退避，超过次数后失败 |
| 响应 schema 变化 | 隔离来源，告警，禁止写入领域模型 |
| 签名上下文不完整 | 本地失败，不发送猜测请求 |
| 游标失效 | 返回 `CURSOR_INVALID`，要求调用方重新搜索 |

## 9. 请求预算和会话配额

第一阶段不建设 Cookie 池和 IP 池，但必须建设请求预算，避免单个会话被无限调用：

```text
per_session_requests_per_minute
per_session_requests_per_hour
per_tenant_requests_per_minute
per_search_max_pages
per_search_max_candidates
per_profile_max_requests
cooldown_seconds
```

默认预算由配置文件控制，具体数值上线前通过小规模实测确定。预算的目的为：

- 限制业务误用；
- 防止调用方无限循环；
- 控制成本和响应时间；
- 遇到异常时快速熔断。

不是为了通过多个 Cookie 或 IP 提高规避能力。

## 10. 统一领域模型

### 10.1 Creator

```text
creator
- id
- platform = tiktok
- platform_user_id
- username
- display_name
- bio
- profile_url
- avatar_url
- region_code
- language
- is_verified
- follower_count
- following_count
- likes_count
- video_count
- bio_url
- first_seen_at
- last_seen_at
- deleted_at
```

### 10.2 Video

```text
video
- id
- platform_video_id
- creator_id
- description
- hashtags
- create_time
- region_code
- view_count
- like_count
- comment_count
- share_count
- video_url
- collected_at
- source
- source_scope
- session_id_hash
```

只保存不可逆的 `session_id_hash` 用于审计关联，不把会话密钥写入业务表。

### 10.3 Provenance

```text
provenance
- entity_type
- entity_id
- source
- source_scope
- authorization_basis
- session_id_hash
- signer_version
- source_record_id
- collected_at
- expires_at
- retention_until
- field_status
- raw_payload_hash
```

`raw_payload_hash` 只用于判断内容是否变化，不保存包含 Cookie、token 或签名的原始请求/响应。

### 10.4 SearchResult

```text
search_result
- request_id
- creator_id
- matched_video_ids
- matched_keywords
- matched_hashtags
- relevance_score
- quality_score
- freshness_score
- total_score
- match_reasons
- generated_at
```

## 11. 去重、聚合和评分

### 11.1 账号规范化

```text
creator_a
@creator_a
https://www.tiktok.com/@creator_a
https://www.tiktok.com/@creator_a?lang=en
```

统一为：

```text
platform = tiktok
username = creator_a
canonical_profile_url = https://www.tiktok.com/@creator_a
```

### 11.2 去重优先级

1. 稳定用户 ID；
2. 稳定视频作者 ID；
3. 标准化主页 URL；
4. `username + platform`；
5. 展示名不能作为唯一键。

### 11.3 作者级聚合

```text
matched_video_count
matched_video_ratio
total_matched_views
average_matched_views
max_matched_views
average_likes
average_comments
average_shares
latest_matched_video_time
matched_keywords
matched_hashtags
```

一次视频命中不能直接判定账号属于目标领域。必须综合：

- 命中视频数量；
- 命中视频比例；
- 作者简介；
- Hashtag；
- 最近发布时间；
- 近期播放和互动表现。

### 11.4 评分

内容相关度：

```text
视频描述命中关键词       30 分
Hashtag 命中             20 分
近 30 天命中视频数量      15 分
命中视频占采样比例        15 分
作者简介命中             10 分
公开定位/bio URL 命中     10 分
```

达人质量：

```text
粉丝规模                 20 分
近期平均播放             20 分
播放/粉丝表现             20 分
互动率                   20 分
持续发布频率             10 分
认证或公开商业信号        10 分
```

公式：

```text
engagement_rate =
    (like_count + comment_count + share_count)
    / max(view_count, 1)

creator_engagement_rate =
    sum(like_count + comment_count + share_count)
    / max(sum(view_count), 1)

total_score =
    0.55 * relevance_score +
    0.35 * quality_score +
    0.10 * freshness_score
```

字段缺失必须返回 `null` 或 `not_available`，不得把缺失值默认为 0。

## 12. SQLite 和缓存

### 12.1 首期直接使用 SQLite

保存：

- Creator；
- Video；
- MetricSnapshot；
- Provenance；
- SearchResult；
- 请求缓存；
- 会话元数据；
- 会话健康状态；
- API 调用审计；
- 删除和撤回状态。

会话敏感字段必须使用应用层加密，推荐将密钥放在系统密钥链或部署平台 Secret 中。若无法保证加密，不保存完整会话上下文，只支持临时内存会话。

### 12.2 缓存规则

缓存键至少包含：

```text
operation
normalized_query
session_scope
signer_version
source_schema_version
```

不能把不同会话产生的非公开会话状态混合缓存。对于公开数据，也必须保留采集时间和来源。

### 12.3 数据删除

支持：

```http
DELETE /internal/v1/tiktok/creators/{creator_id}
POST   /internal/v1/tiktok/sessions/{session_id}/revoke
POST   /internal/v1/tiktok/data/purge
```

删除会清理：

- 创作者记录；
- 视频关联；
- 来源记录；
- 缓存；
- 搜索结果引用；
- 会话关联的敏感数据。

## 13. 代码结构

```text
/Users/wuwenbo/Documents/TKCralw/Scout/
├── app/
│   ├── api/
│   │   ├── app.py
│   │   ├── auth.py
│   │   ├── schemas.py
│   │   └── errors.py
│   ├── sessions/
│   │   ├── models.py
│   │   ├── importers.py
│   │   ├── vault.py
│   │   ├── health.py
│   │   └── revoke.py
│   ├── signing/
│   │   ├── base.py
│   │   ├── web_signer.py
│   │   ├── request_contract.py
│   │   ├── versions.py
│   │   └── errors.py
│   ├── transport/
│   │   ├── public_readonly.py
│   │   ├── request_templates.py
│   │   ├── response_classifier.py
│   │   └── pagination.py
│   ├── discovery/
│   │   ├── base.py
│   │   ├── tiktok_public_search.py
│   │   ├── submitted_profiles.py
│   │   └── resolver.py
│   ├── domain/
│   │   ├── models.py
│   │   ├── normalization.py
│   │   ├── deduplication.py
│   │   ├── aggregation.py
│   │   └── scoring.py
│   ├── governance/
│   │   ├── provenance.py
│   │   ├── retention.py
│   │   └── deletion.py
│   └── storage/
│       ├── sqlite.py
│       ├── repositories.py
│       └── migrations/
├── tests/
│   ├── fixtures/
│   ├── test_api_contract.py
│   ├── test_session_vault.py
│   ├── test_signer_contract.py
│   ├── test_request_templates.py
│   ├── test_response_classifier.py
│   ├── test_normalization.py
│   ├── test_deduplication.py
│   ├── test_aggregation.py
│   ├── test_scoring.py
│   ├── test_persistence.py
│   └── test_security_boundaries.py
└── docs/
```

### 13.1 实现约束

- API schema 与数据库 schema 分离；
- API 调用方不接触上游 Cookie 和签名上下文；
- 生产配置只加载只读请求模板；
- `signing` 层不能调用写接口；
- 所有外部响应先经过 schema 校验和分类，再进入领域模型；
- 不把 TiktokApis 代码直接复制进 `app/signing/`；
- TiktokApis 没有 LICENSE，默认不复制、不打包、不作为运行时依赖；
- 签名器和会话存储必须支持版本和撤销。

## 14. 分阶段执行计划

### Phase 0：会话和请求证据准备

目标：确认当前授权浏览器会话能够稳定支持目标只读请求。

任务：

1. 明确只读数据范围和禁止请求范围；
2. 通过用户自己的受控浏览器获取脱敏请求证据；
3. 列出搜索、主页、视频列表和详情所需上下文；
4. 建立 request template registry；
5. 确认签名输入、header contract 和分页字段；
6. 建立会话加密、撤销和健康状态设计；
7. 为每个请求定义成功、权限失败、挑战和 schema 变化处理。

交付物：

```text
session-context-spec.md
readonly-request-registry.md
signer-contract.md
response-schema-fixtures/
credential-policy.md
```

完成条件：

- 不使用第三方 Cookie；
- 不涉及写接口；
- 不把完整真实会话提交 Git；
- 目标只读请求可以在受控环境中完成字段验证；
- 缺少上下文时会安全失败；
- 遇到挑战时会停止，不自动绕过。

### Phase 1：领域模型 + 同步 API MVP

目标：完成可调用的最小闭环。

任务：

1. 建立 Session、Creator、Video、Provenance、SearchResult 模型；
2. 建立 SQLite schema 和 repository；
3. 实现 `Signer`、`Transport` 和请求模板；
4. 实现会话导入、加密保存、健康检查和撤销；
5. 实现公开搜索请求；
6. 实现主页和视频列表读取；
7. 实现账号规范化、去重和作者聚合；
8. 实现关键词、Hashtag、排除词、地区和粉丝过滤；
9. 实现评分和命中原因；
10. 实现同步搜索 API、账号补全 API 和详情 API；
11. 实现 API Key 鉴权、请求预算和审计。

完成条件：

- 可以从授权会话完成“搜索 → 候选视频 → 作者聚合 → 评分 → JSON 返回”；
- 可以处理用户提交的 TikTok 主页；
- 同一作者不会重复返回；
- 每条结果包含来源、签名器版本和采集时间；
- SQLite 重启后数据、缓存和审计仍存在；
- Cookie、token、storage 和签名字段不进入对外响应；
- 单元测试、合约测试和安全边界测试通过。

### Phase 2：稳定性和数据质量

目标：确认自建签名采集器在受控范围内可持续运行。

任务：

- 添加请求预算和熔断；
- 添加会话过期检测；
- 添加响应 schema 版本；
- 添加分页重复检测；
- 添加字段缺失率统计；
- 添加 Top 20 人工评审集；
- 完善评分版本和匹配解释；
- 添加数据刷新和过期策略。

完成条件：

- 429、挑战、登录失效和 schema 变化不会导致无限重试；
- 同一查询在缓存窗口内不会重复发起无意义请求；
- Top 20 人工相关率达到试运行目标；
- 结果具有可解释的匹配原因；
- 会话撤销后不能继续产生新请求。

### Phase 3：多会话租户隔离（可选）

只有在 Phase 1-2 稳定后，才评估多会话支持。

允许的方向：

- 不同客户使用各自明确授权的会话；
- 会话按租户隔离；
- 每个租户有独立请求预算；
- 会话健康、撤销和审计独立管理。

不允许演变为：

- 共享 Cookie 池；
- 购买账号池；
- 代理/IP 轮换；
- 设备指纹池；
- 自动化规避平台限制。

### Phase 4：异步搜索任务

前置条件：

- SQLite 缓存稳定；
- 请求预算和会话状态机稳定；
- 同步 API 已有真实调用方；
- 搜索耗时确实超过同步接口可接受范围。

任务：

- 引入任务队列；
- 实现 job 状态机；
- 实现断点续跑；
- 实现来源级配额调度；
- 实现取消、失败重试和死信处理。

### Phase 5：调用方集成和 Web CRM

在 API 稳定并完成业务验证后，再考虑：

- 多租户控制台；
- 创作者收藏、标签和跟进状态；
- 团队权限；
- Web CRM；
- 全文/向量搜索；
- 业务数据分析。

## 15. 测试 Spec

### 15.1 签名和会话测试

必须覆盖：

- 会话上下文完整性；
- 会话撤销后请求被拒绝；
- Cookie、token 和 storage 不出现在日志；
- URL 编码和参数顺序；
- 请求体编码；
- header contract；
- 签名版本；
- 缺少上下文时本地失败；
- 旧签名不能直接重放；
- 只读模板无法调用写接口。

### 15.2 Transport 测试

使用脱敏 fixture 和 mock server：

- 搜索结果解析；
- 主页解析；
- 视频列表解析；
- 空结果；
- 重复结果；
- 游标失效；
- 401/403；
- 429；
- 5xx；
- 验证码/WAF 挑战；
- schema 变化。

### 15.3 领域测试

- 关键词和 Hashtag 规范化；
- URL、用户名和平台 ID 去重；
- 日期、地区和粉丝过滤；
- 视频到作者聚合；
- 评分公式和边界；
- 缺失字段；
- 多来源/多时间快照；
- SQLite 重启恢复；
- 缓存隔离；
- 删除和撤回。

### 15.4 API 合约测试

- `request_id` 和 `schema_version`；
- 分页游标；
- 错误码；
- API Key 鉴权；
- 速率限制；
- 幂等键；
- 响应不泄露会话信息；
- 响应包含来源、签名器版本和采集时间。

### 15.5 业务验收测试

至少准备三类关键词：

1. 单一垂直领域：`fitness coach`；
2. 中英文混合：`健身 / workout`；
3. 歧义词或品牌名。

每类评估：

- Top 10 和 Top 20 相关率；
- 一次命中账号是否被错误判定为垂直达人；
- 粉丝量与互动率排序；
- 命中原因解释；
- 结果来源、签名器版本和更新时间；
- 会话失效后的停止行为；
- 删除后不再返回已删除数据。

## 16. 可观测指标

```text
api_requests_total
api_errors_total
session_active_total
session_expired_total
session_reauth_required_total
signer_success_total
signer_failure_total
source_requests_total
source_rate_limited_total
source_challenge_total
source_schema_changed_total
videos_discovered_total
creators_discovered_total
creators_deduplicated_total
creator_profile_completion_rate
keyword_match_rate
api_response_rows_total
cache_hit_rate
stale_records_total
```

安全告警：

```text
unexpected_write_path_attempt
arbitrary_path_attempt
signature_input_from_external_client
session_context_logged
session_used_after_revoke
rate_limit_bypass_pattern
```

## 17. 第一阶段 Definition of Done

Phase 1 只有同时满足以下条件才算完成：

- [ ] 使用用户明确授权的浏览器会话；
- [ ] 只读请求模板已登记；
- [ ] 自建签名模块可以基于当前请求和会话上下文动态生成签名；
- [ ] 不复放静态抓包签名；
- [ ] 不接受外部传入 Cookie、storage、设备参数或签名字段；
- [ ] 不支持写接口；
- [ ] 不使用 Cookie 池、IP 池、设备池或代理轮换规避限制；
- [ ] 已有 Session、Creator、Video、Provenance、SearchResult 模型；
- [ ] 使用 SQLite 保存业务数据、缓存和审计；
- [ ] 可以按关键词、Hashtag、排除词、地区和粉丝区间筛选；
- [ ] 可以计算并解释相关度、质量和新鲜度分；
- [ ] 可以按稳定 ID、主页 URL 或 username 去重；
- [ ] 每条结果包含 `source`、`source_scope`、`session_id_hash`、`signer_version` 和 `collected_at`；
- [ ] 返回稳定版本化 JSON schema；
- [ ] 401/403、验证码、429 和 schema 变化能够安全停止；
- [ ] 会话撤销后不能继续发起请求；
- [ ] API 合约、签名、会话、领域和安全测试通过；
- [ ] 业务验收样例可以复现并解释结果。

## 18. 推荐立即执行顺序

```text
1. 明确授权会话的来源、所有者和可用范围
2. 建立 session-context-spec 和 readonly-request-registry
3. 设计会话加密、撤销和健康检查
4. 建立 Signer / Transport 接口
5. 使用脱敏证据实现一个只读公开请求模板
6. 实现 SQLite schema 和 repository
7. 实现搜索、主页和视频列表解析
8. 实现 Creator/Video/Provenance/SearchResult 模型
9. 实现账号规范化、去重、聚合和评分
10. 实现同步搜索 API 和已知账号补全 API
11. 加入请求预算、熔断和错误停止策略
12. 完成签名、会话、API 合约和安全边界测试
13. 小规模人工验证搜索质量
14. Phase 1 通过后再评估多会话和异步任务
```

## 19. 研究参考与代码使用边界

- Scout：<https://github.com/kiryano/Scout>
- TiktokApis：<https://github.com/cv-cat/TiktokApis>

Scout 可复用其 CLI、字段整理、批处理和资料处理思路。TiktokApis 可用于理解请求对象、浏览器上下文和字段结构，但其仓库没有 LICENSE，默认不复制、不打包、不作为运行时依赖。生产实现必须重新建立自己的模块、测试和安全边界。

所有实际运行必须以用户明确授权的会话、公开数据范围、适用平台规则和项目用途为前提。遇到访问挑战、权限拒绝、会话失效或接口变化时，系统停止并等待重新授权或人工处理，不自动绕过。

## 20. 实测验证记录（2026-09-29）

> 本节为 Phase 0 的实机验证补充记录。所有结论均在开发机（macOS，Chrome v153，会话来自用户本人浏览器）实测得出。

### 20.1 会话提取：已打通并工具化

- 工具：`Scout/tools/extract_tiktok_session.py`，从 Chrome Default profile 提取 `.tiktok.com` 全部 cookie，解密后写入 `Scout/.secrets/tiktok_session.json`（chmod 600，不入 Git）。
- 解密方案（Chrome 130+，实测 v153）：v10 前缀 + AES-128-CBC，key = PBKDF2-HMAC-SHA1(钥匙串 "Chrome Safe Storage" 密码, salt=`saltysalt`, 1003 轮, 16B)，IV = 16 空格；**Cookies DB `meta.version>=24` 时，解密明文前 32 字节为 SHA256(domain)，必须去掉**。
- 提取到的登录态：sessionid/sid_tt（至 2027-01）、sid_guard（至 2027-07）、msToken（至 2026-12）、ttwid（至 2027-09）等 35 条。完整登录态确认。

### 20.2 搜索 API：无需签名即可用（对 §5 的影响）

实测 `/api/search/general/full/`（keyword=fitness coach, offset=0）：

```text
HTTP 200, status_code=0
14 items（13 视频 + 1 用户卡），has_more=1, cursor=12
每条含 item.author(uniqueId/verified/signature) +
       item.authorStats(followerCount/heartCount/videoCount) +
       item.statsV2(playCount/diggCount/commentCount/shareCount) +
       item.desc / createTime / challenges
```

成功条件（三者缺一不可）：

1. 真实浏览器会话 cookie（含 ttwid/msToken；匿名返回 200 空 body）；
2. Chrome TLS 指纹（curl_cffi `impersonate="chrome131"`；httpx/curl 裸指纹被 Slardar WAF 拦）；
3. 干净出口 IP（用户本机系统代理 127.0.0.1:7892；沙箱共享出口被拦）。

**影响**：首期搜索发现链路可以不实现 §5 签名模块即跑通 MVP。签名模块降级为"端点收紧后的后备方案"，Phase 1 不阻塞。

### 20.3 不可用链路（含 2026-09-26 结论，仍然有效）

| 链路 | 结论 |
|---|---|
| 匿名 SSR 抓搜索页 | 死路：SSR HTML 无任何结果数据，全部客户端加载 |
| profile SSR 页面（`/@user`） | 即使带会话+导航头，仍被 Slardar WAF JS 挑战拦（1462 字节壳）→ 作者资料从搜索结果内嵌字段获取 |
| 旧版 `/api/user/detail/` | 200 空 body，已废 |
| 匿名调搜索 API | 200 空 body，静默拒绝 |

### 20.4 频率与风控观测

- 搜索 API 有 **IP 级软冷却**：短时间多次请求后，同 IP 匿名+会话请求均返回 200 空 body（非 429），冷却持续数分钟量级。
- 生产约束：单关键词单次搜索 + 指数退避；WAF 挑战页（`slardar` 标记）触发即停止，不重试不绕过。

### 20.5 交付物路径

```text
Scout/tools/extract_tiktok_session.py            会话提取（一键重跑）
Scout/.secrets/tiktok_session.json               会话材料（chmod 600）
Scout/app/discovery/web_client.py                已验证的 Discovery 客户端（search/health/quota + 限速 + WAF 即停）
Scout/tests/fixtures/search_general_fitness_coach.json   真实响应 fixture（已脱敏校验）
```

### 20.6 遗留待办

- [ ] 软冷却窗口时长标定（退避曲线）；
- [ ] 翻页 search_id 机制验证（首测 cursor/has_more 正常）；
- [ ] 资料补全路径确认：搜索内嵌 author 字段够不够，或找可用的无签名 profile 端点；
- [ ] msToken 2026-12 到期前重提取（工具已支持一键重跑）。
