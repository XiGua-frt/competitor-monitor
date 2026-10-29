"""Scout 内部 API 的真实启动入口：``python -m app.api``。

装配顺序（全部来自已验证的模块，本文件只做组装，不含业务逻辑）：

    会话文件 → import_session（生成不可逆 hash 元数据）
    → InMemorySessionVault（仅内存，明文材料不落盘）
    → Repository.connect（SQLite 业务库）
    → TikTokWebClient（curl_cffi chrome131 + 本机代理 + 会话）
    → PublicReadonlySearch（只读适配层：分页上限/WAF 即停/无轮换）
    → DiscoveryService（解析 → 去重 → 聚合 → 评分 → 持久化 → 审计）
    → ApiApp + make_http_handler → HTTPServer

设计边界（不可配置、不可绕过）：
- 单进程串行 ``HTTPServer``：内部低频使用，idempotency/预算计数器非线程安全，
  明确不开多线程；要更高吞吐属于 Phase 2 的改造，不是调参问题。
- 只绑定本机回环（默认 127.0.0.1）：本 API 不是对外服务，放在反代/公网之后
  属于误用；需要远程访问时由调用方自己加 VPN/SSH 隧道。
- API Key 只从环境变量读取（``SCOUT_API_KEY`` / ``SCOUT_API_KEY`` 复数形式
  ``SCOUT_API_KEYS``），启动时校验缺失即退出，绝不提供默认 Key。

环境变量：

    SCOUT_API_KEY / SCOUT_API_KEYS   Bearer Key（必填）
    TIKTOK_SESSION_FILE              会话 JSON 路径（默认 .secrets/tiktok_session.json）
    SCOUT_DB                         SQLite 路径（默认 ./scout.sqlite3）
    SCOUT_HOST / SCOUT_PORT          监听地址（默认 127.0.0.1:8787）
    SCOUT_PROXY                      出口代理（默认本机 127.0.0.1:7892）
"""

from __future__ import annotations

import os
import signal
import sys
from http.server import HTTPServer

from .app import ApiApp, VaultSessionProvider, make_http_handler
from .auth import ApiKeyAuth, RequestBudget
from .errors import SCHEMA_VERSION  # noqa: F401 - re-export 保持单一来源由 errors 负责
from app.discovery.service import DiscoveryService
from app.sessions.importers import import_session
from app.sessions.vault import InMemorySessionVault
from app.storage.repositories import Repository
from app.transport.public_readonly import PublicReadonlySearch

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_DB = "scout.sqlite3"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def build_server() -> HTTPServer:
    """按固定装配顺序构建 HTTPServer；任何一步失败都是启动期错误，直接退出。"""
    host = _env("SCOUT_HOST", DEFAULT_HOST)
    port = int(_env("SCOUT_PORT", str(DEFAULT_PORT)))
    db_path = _env("SCOUT_DB", DEFAULT_DB)
    session_file = _env("TIKTOK_SESSION_FILE")  # 为 None 时 web_client 用 .secrets 默认路径

    auth = ApiKeyAuth()  # 内部读 SCOUT_API_KEY(S)，缺失时抛 ValueError
    meta = import_session(session_file) if session_file else import_session(
        _default_session_file())
    vault = InMemorySessionVault(meta)

    repository = Repository.connect(db_path)
    transport = PublicReadonlySearch.from_verified_client(
        session_file=session_file,
        proxy=_env("SCOUT_PROXY"),  # None 时 web_client 走 default_proxy()
    )
    service = DiscoveryService(transport, repository, session_id_hash=meta.session_id_hash)

    api = ApiApp(
        service=service,
        repository=repository,
        auth=auth,
        budget=RequestBudget(),
        sessions=VaultSessionProvider(vault, meta.session_id),
    )
    return HTTPServer((host, port), make_http_handler(api))


def _default_session_file() -> str:
    """与 web_client.DEFAULT_SESSION_FILE 同口径的默认会话路径。"""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, ".secrets", "tiktok_session.json")


def main() -> int:
    try:
        server = build_server()
    except ValueError as exc:  # API Key 未配置等启动期配置错误
        print(f"[scout-api] 启动失败：{exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(
            "[scout-api] 启动失败：找不到会话文件。"
            "请先运行 tools/extract_tiktok_session.py 提取你本人的浏览器会话，"
            f"或用 TIKTOK_SESSION_FILE 指定路径。({exc})",
            file=sys.stderr,
        )
        return 2

    host, port = server.server_address[:2]
    print(
        f"[scout-api] listening on http://{host}:{port}  "
        "routes: POST /v1/tiktok/creator-search | POST /v1/tiktok/profiles/resolve | "
        "GET|POST /internal/v1/tiktok/sessions/{id}/status|revoke"
    )
    print(
        "[scout-api] 边界：仅限本机内部低频调用；会话材料只在进程内存；"
        "WAF 挑战即停不重试。Ctrl+C 退出。"
    )

    def _shutdown(signum, _frame):  # noqa: ANN001 - signal handler 约定
        print(f"[scout-api] 收到信号 {signum}，正在退出（等待在途请求结束）…")
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _shutdown)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("[scout-api] 已退出。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
