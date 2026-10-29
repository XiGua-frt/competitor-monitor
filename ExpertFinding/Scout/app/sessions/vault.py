"""会话 vault：默认只在进程内存里保存状态与（可选的）明文材料。

不提供任何写盘入口：明文 Cookie / msToken 不会被持久化，也不会出现在
repr、日志或 checkpoint 快照里。健康检查通过注入 probe 完成，遇到 WAF
只返回停止状态，不重试。
"""

from __future__ import annotations

from typing import Any, Mapping

from .models import HealthStatus, SessionHealthStatus, SessionMeta, SessionStatus
from .revoke import (
    SessionNotFoundError,
    assert_active,
    is_expired,
    is_revoked,
)

# 出现这些字样即判定为 WAF 挑战：停止任务并交人工处理，绝不重试
WAF_MARKERS = ("waf", "slardar", "challenge", "captcha", "verify")


class InMemorySessionVault:
    """进程内会话登记簿：状态可查、可撤销，材料默认不落盘。"""

    def __init__(self, *sessions: SessionMeta):
        self._sessions: dict[str, SessionMeta] = {}
        self._material: dict[str, dict[str, str]] = {}
        for meta in sessions:
            self.put(meta)

    def put(self, meta: SessionMeta) -> None:
        self._sessions[meta.session_id] = meta

    def get(self, session_id: str) -> SessionMeta:
        try:
            return self._sessions[session_id]
        except KeyError:
            raise SessionNotFoundError(f"会话未登记: {session_id}") from None

    def revoke(self, session_id: str) -> SessionMeta:
        """撤销会话：状态置为 revoked，并丢弃内存中的明文材料。"""
        meta = self.get(session_id)
        revoked = meta.with_status(SessionStatus.REVOKED)
        self._sessions[session_id] = revoked
        self._material.pop(session_id, None)
        return revoked

    def require_active(self, session_id: str) -> SessionMeta:
        meta = self.get(session_id)
        assert_active(meta)
        return meta

    def store_material(self, session_id: str, cookies: Mapping[str, str]) -> None:
        """仅在内存中持有明文材料（不写盘、不进 repr）。"""
        self.get(session_id)  # 未登记的会话不能存材料
        self._material[session_id] = dict(cookies)

    def material(self, session_id: str) -> Mapping[str, str]:
        """取出内存中的明文材料；撤销/过期的会话一律拒绝。"""
        assert_active(self.get(session_id))
        try:
            return self._material[session_id]
        except KeyError:
            raise SessionNotFoundError(
                f"会话材料未载入或已清除: {session_id}") from None

    def active(self) -> tuple[SessionMeta, ...]:
        return tuple(m for m in self._sessions.values()
                     if not is_revoked(m) and not is_expired(m))

    def checkpoint(self) -> list[dict[str, Any]]:
        """可安全落盘/传输的快照：只有元数据。"""
        return [m.public_status() for m in self._sessions.values()]

    def __len__(self) -> int:
        return len(self._sessions)

    def __repr__(self) -> str:
        return f"InMemorySessionVault(sessions={len(self._sessions)})"


def _waf_challenge(result: Mapping[str, Any]) -> bool:
    if result.get("waf") or result.get("challenge"):
        return True
    # 真实 probe（TikTokWebClient.health）只在 status 通道上报分类结论，
    # 形如 {"ok": False, "http": 200, "status": "waf_challenge"}，不含 error/detail
    status = str(result.get("status") or "").lower()
    if any(marker in status for marker in WAF_MARKERS):
        return True
    text = str(result.get("error") or result.get("detail") or "").lower()
    return any(marker in text for marker in WAF_MARKERS)


def check_health(meta: SessionMeta, probe: Any) -> SessionHealthStatus:
    """对单个会话做一次健康检查（调用方注入 probe，例如 TikTokWebClient）。

    - 已撤销 / 已过期：直接返回停止状态，不触碰网络；
    - WAF 挑战：返回 stopped 状态，只探测一次，绝不重试绕过。
    """
    if is_revoked(meta):
        return SessionHealthStatus(meta.session_id, HealthStatus.REVOKED,
                                   ok=False, stopped=True,
                                   detail="会话已撤销，未发起健康检查")
    if is_expired(meta):
        return SessionHealthStatus(meta.session_id, HealthStatus.EXPIRED,
                                   ok=False, stopped=True,
                                   detail="会话已过期，请重新提取")

    result = probe.health() or {}
    ok = bool(result.get("ok"))
    if ok:
        return SessionHealthStatus(meta.session_id, HealthStatus.HEALTHY,
                                   ok=True, stopped=False,
                                   detail=f"http={result.get('http')}")
    if _waf_challenge(result):
        return SessionHealthStatus(meta.session_id, HealthStatus.WAF_CHALLENGE,
                                   ok=False, stopped=True,
                                   detail="WAF 挑战：停止任务，更换干净出口后人工处理")
    return SessionHealthStatus(meta.session_id, HealthStatus.UNHEALTHY,
                               ok=False, stopped=True,
                               detail=str(result.get("error") or "health check failed")[:120])


__all__ = ["InMemorySessionVault", "check_health"]
