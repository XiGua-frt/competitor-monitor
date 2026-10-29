"""Pure AWS Signature Version 4 used by TikTok Creator Studio's VOD API.

TikTok's ``/top/v1`` upload endpoints use temporary credentials returned by
``/api/v1/video/upload/auth/``.  The browser signs only the ``x-amz-*``
headers; cookies and browser client hints are deliberately outside the AWS
canonical request.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from urllib.parse import quote


REGION = "ap-singapore-1"
SERVICE = "vod"
ALGORITHM = "AWS4-HMAC-SHA256"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hmac(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()


def _pairs(values: Mapping | Iterable[tuple[object, object]]):
    source = values.items() if hasattr(values, "items") else values
    return [(str(key), "" if value is None else str(value)) for key, value in source]


def canonical_query(values: Mapping | Iterable[tuple[object, object]]) -> str:
    """Return the RFC 3986 encoded, byte-sorted SigV4 query string."""
    encoded = [
        (quote(key, safe="-_.~"), quote(value, safe="-_.~"))
        for key, value in _pairs(values)
    ]
    encoded.sort()
    return "&".join(f"{key}={value}" for key, value in encoded)


def sign(
    *,
    method: str,
    path: str,
    query: Mapping | Iterable[tuple[object, object]],
    access_key_id: str,
    secret_access_key: str,
    session_token: str,
    body: bytes | str = b"",
    amz_date: str | None = None,
    region: str = REGION,
    service: str = SERVICE,
) -> dict[str, str]:
    """Pure-compute the exact SigV4 headers used by Creator Studio.

    ``amz_date`` is injectable for browser-curl comparison tests.  No value is
    padded, truncated, cached, or read from ambient AWS configuration.
    """
    if not access_key_id or not secret_access_key or not session_token:
        raise ValueError("AWS V4 缺少 access key、secret key 或 session token")
    if not path.startswith("/"):
        raise ValueError("AWS V4 path 必须以 / 开头")
    if isinstance(body, str):
        payload = body.encode("utf-8")
    elif isinstance(body, (bytes, bytearray, memoryview)):
        payload = bytes(body)
    else:
        raise TypeError("AWS V4 body 必须是 str 或 bytes-like")
    if amz_date is None:
        amz_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if len(amz_date) != 16 or not amz_date.endswith("Z"):
        raise ValueError("x-amz-date 必须是 YYYYMMDDTHHMMSSZ")

    date = amz_date[:8]
    payload_hash = _sha256(payload)
    signed_names = ["x-amz-date", "x-amz-security-token"]
    canonical_headers = (
        f"x-amz-date:{amz_date}\n"
        f"x-amz-security-token:{session_token}\n"
    )
    if payload:
        signed_names.insert(0, "x-amz-content-sha256")
        canonical_headers = (
            f"x-amz-content-sha256:{payload_hash}\n" + canonical_headers
        )
    signed_headers = ";".join(signed_names)
    canonical_request = "\n".join((
        method.upper(), quote(path, safe="/-_.~"), canonical_query(query),
        canonical_headers, signed_headers, payload_hash,
    ))
    scope = f"{date}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join((
        ALGORITHM, amz_date, scope,
        _sha256(canonical_request.encode("utf-8")),
    ))
    date_key = _hmac(("AWS4" + secret_access_key).encode("utf-8"), date)
    region_key = _hmac(date_key, region)
    service_key = _hmac(region_key, service)
    signing_key = _hmac(service_key, "aws4_request")
    signature = hmac.new(
        signing_key, string_to_sign.encode("utf-8"), hashlib.sha256,
    ).hexdigest()
    authorization = (
        f"{ALGORITHM} Credential={access_key_id}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    headers = {
        "authorization": authorization,
        "x-amz-security-token": session_token,
        "x-amz-date": amz_date,
    }
    if payload:
        headers["x-amz-content-sha256"] = payload_hash
    return headers


__all__ = ["ALGORITHM", "REGION", "SERVICE", "canonical_query", "sign"]
