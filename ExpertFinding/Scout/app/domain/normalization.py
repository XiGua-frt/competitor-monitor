"""Canonical forms for usernames, profile URLs, keywords, hashtags and sessions.

Every function here is pure: same input, same output, no I/O. The canonical
forms are what the deduplication layer is allowed to key on later.
"""

from __future__ import annotations

import hashlib
import re

PLATFORM = "tiktok"
PROFILE_URL_TEMPLATE = "https://www.tiktok.com/@{username}"

_SESSION_HASH_PREFIX = "sha256:"
_USERNAME_CHARS = re.compile(r"[^0-9a-z_.]")
_HASHTAG_CHARS = re.compile(r"[^0-9a-z_]")
_WHITESPACE = re.compile(r"\s+")
_PROFILE_USERNAME = re.compile(r"tiktok\.com/@([0-9A-Za-z_.\-]+)")


def normalize_username(value: str | None) -> str:
    """Return the lowercase handle without any ``@`` or URL decoration."""
    if not isinstance(value, str):
        return ""

    candidate = value.strip()
    match = _PROFILE_USERNAME.search(candidate)
    if match:
        candidate = match.group(1)
    candidate = candidate.strip().lstrip("@")

    # A bare URL without "@handle" (or any other junk) must not leak through.
    if "/" in candidate or "?" in candidate:
        candidate = candidate.split("?", 1)[0].rsplit("/", 1)[-1]

    return _USERNAME_CHARS.sub("", candidate.lower())


def canonical_profile_url(value: str | None) -> str:
    """Return the single canonical profile URL for a handle or profile URL."""
    username = normalize_username(value)
    if not username:
        return ""
    return PROFILE_URL_TEMPLATE.format(username=username)


def normalize_hashtag(value: str | None) -> str:
    """Return the lowercase hashtag name without ``#`` or padding."""
    if not isinstance(value, str):
        return ""
    candidate = value.strip().lstrip("#").strip()
    return _HASHTAG_CHARS.sub("", candidate.lower())


def normalize_keyword(value: str | None) -> str:
    """Return the whitespace-collapsed, casefolded search keyword."""
    if not isinstance(value, str):
        return ""
    return _WHITESPACE.sub(" ", value.strip()).lower()


def hash_session_id(session_id: str | None) -> str:
    """Return an irreversible session reference.

    The plaintext session identifier never leaves this function: callers store
    and pass around only the ``sha256:<hex>`` form.
    """
    if not isinstance(session_id, str):
        session_id = "" if session_id is None else str(session_id)
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return f"{_SESSION_HASH_PREFIX}{digest}"


def is_hashed_session_id(value: str | None) -> bool:
    """True when *value* already is a ``sha256:<64 hex>`` session reference."""
    if not isinstance(value, str):
        return False
    prefix, _, digest = value.partition(":")
    if prefix != _SESSION_HASH_PREFIX.rstrip(":") or len(digest) != 64:
        return False
    return all(char in "0123456789abcdef" for char in digest)
