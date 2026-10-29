"""会话导入：读取 tools/extract_tiktok_session.py 的输出并生成元数据。

只返回 SessionMeta：明文 Cookie / msToken 不会离开本模块的局部变量，
也不会进入日志、返回值或任何 DTO。
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Mapping

from .models import SessionMeta, SessionStatus

# 登录态判定沿用 tools/extract_tiktok_session.py 的口径
LOGIN_COOKIES = ("sessionid", "sessionid_ss", "sid_tt", "multi_sids")
HASH_DOMAIN = b"scout/session-id/v1"


class SessionImportError(RuntimeError):
    """会话文件缺失、损坏或登录态不完整。"""


def session_id_hash(login_value: str) -> str:
    """不可逆 hash：只用于标识会话，无法反推 Cookie 明文。"""
    return hashlib.sha256(HASH_DOMAIN + login_value.encode("utf-8")).hexdigest()


def _login_value(cookies: Mapping[str, Any]) -> str:
    for name in LOGIN_COOKIES:
        value = cookies.get(name)
        if value:
            return str(value)
    return ""


def _earliest_expiry(expires: Mapping[str, Any] | None,
                     cookies: Mapping[str, Any]) -> str | None:
    """取登录态 cookie 中最早的过期时间；没有过期信息则为 None。"""
    candidates = [str(expires[name]) for name in LOGIN_COOKIES
                  if (expires or {}).get(name) and name in cookies]
    return min(candidates) if candidates else None


def import_session(path: str | os.PathLike) -> SessionMeta:
    """导入会话文件，返回不含明文凭证的元数据。

    校验口径与 app/discovery/web_client.load_session() 一致：
    必须有 sessionid/sessionid_ss/sid_tt 之一，且必须有 msToken。
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise SessionImportError(f"会话文件不可读或不是合法 JSON: {e}") from e

    if not isinstance(data, dict):
        raise SessionImportError("会话文件格式不正确：顶层应为对象")

    cookies = data.get("cookies")
    if not isinstance(cookies, dict) or not cookies:
        raise SessionImportError("会话文件缺少 cookies")

    login_value = _login_value(cookies)
    if not login_value:
        raise SessionImportError("会话材料缺失登录态（sessionid/sid_tt），请重新提取")
    if not cookies.get("msToken"):
        raise SessionImportError("会话材料缺失 msToken，请重新提取")

    digest = session_id_hash(login_value)
    return SessionMeta(
        session_id=f"tiktok:{digest[:12]}",
        session_id_hash=digest,
        source=str(data.get("profile") or os.path.basename(str(path))),
        status=SessionStatus.ACTIVE,
        imported_at=datetime.now(timezone.utc).isoformat(),
        expires_at=_earliest_expiry(data.get("expires"), cookies),
    )
