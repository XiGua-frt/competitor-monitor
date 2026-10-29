"""撤销 / 到期状态检查：在任何网络调用之前阻断 transport。"""

from __future__ import annotations

from .models import SessionMeta, SessionStatus


class SessionRevokedError(RuntimeError):
    """会话已被撤销：禁止继续使用，禁止发起任何请求。"""


class SessionExpiredError(RuntimeError):
    """会话已过期：需要用户重新提取后才能继续。"""


class SessionNotFoundError(RuntimeError):
    """会话未在 vault 中登记。"""


def is_revoked(meta: SessionMeta) -> bool:
    return meta.status == SessionStatus.REVOKED


def is_expired(meta: SessionMeta) -> bool:
    return meta.is_expired()


def is_active(meta: SessionMeta) -> bool:
    return not is_revoked(meta) and not is_expired(meta)


def assert_active(meta: SessionMeta) -> None:
    if is_revoked(meta):
        raise SessionRevokedError(
            f"会话已撤销，禁止继续使用: {meta.session_id}")
    if is_expired(meta):
        raise SessionExpiredError(
            f"会话已过期，请重新提取: {meta.session_id}")


def assert_transport_allowed(meta: SessionMeta) -> None:
    """transport 出口闸门：撤销/过期的会话在发出请求前就被拦下。"""
    assert_active(meta)
