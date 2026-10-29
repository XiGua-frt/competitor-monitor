"""会话元数据模型：只承载状态/来源/到期/hash，绝不承载明文凭证。

会话文件格式对齐 tools/extract_tiktok_session.py：
    {"extracted_at": iso, "profile": str, "cookies": {name: value}}
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# 状态取值使用普通字符串常量，避免 enum 出现在 repr / JSON DTO 中
class SessionStatus:
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


# 健康检查状态
class HealthStatus:
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    WAF_CHALLENGE = "waf_challenge"
    REVOKED = "revoked"
    EXPIRED = "expired"


def parse_iso(value: str | None) -> datetime | None:
    """解析 ISO 时间，失败返回 None（不抛异常打断状态判定）。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class SessionMeta:
    """会话元数据。字段里没有 Cookie 名或值。"""

    session_id: str
    session_id_hash: str
    source: str
    status: str = SessionStatus.ACTIVE
    imported_at: str = ""
    expires_at: str | None = None

    def public_status(self) -> dict[str, Any]:
        """对外/落盘安全的 DTO：状态、来源、过期时间和不可逆 hash。"""
        return {
            "session_id": self.session_id,
            "session_id_hash": self.session_id_hash,
            "source": self.source,
            "status": self.status,
            "imported_at": self.imported_at,
            "expires_at": self.expires_at,
        }

    def is_expired(self, now: datetime | None = None) -> bool:
        expires = parse_iso(self.expires_at)
        if expires is None:
            return False
        return (now or datetime.now(timezone.utc)) >= expires

    def with_status(self, status: str) -> SessionMeta:
        return SessionMeta(
            session_id=self.session_id,
            session_id_hash=self.session_id_hash,
            source=self.source,
            status=status,
            imported_at=self.imported_at,
            expires_at=self.expires_at,
        )


@dataclass(frozen=True)
class SessionContext:
    """传给 Signer 的最小会话上下文：只有标识和状态，没有凭证。"""

    session_id: str
    session_id_hash: str
    status: str = SessionStatus.ACTIVE

    @classmethod
    def from_meta(cls, meta: SessionMeta) -> SessionContext:
        return cls(session_id=meta.session_id,
                   session_id_hash=meta.session_id_hash,
                   status=meta.status)


@dataclass(frozen=True)
class SessionHealthStatus:
    """健康检查结论。detail 是截断后的短文本，不含凭证。"""

    session_id: str
    status: str
    ok: bool
    stopped: bool
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "ok": self.ok,
            "stopped": self.stopped,
            "detail": self.detail,
        }
