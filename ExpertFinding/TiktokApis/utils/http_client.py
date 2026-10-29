"""Small HTTP adapter with Chrome-like TLS when curl_cffi is installed."""

from __future__ import annotations

import os

try:
    from curl_cffi import requests as _curl_requests
except ImportError:  # pragma: no cover - exercised only in minimal installs
    _curl_requests = None
import requests as _requests


def _kwargs(kwargs):
    values = dict(kwargs)
    if _curl_requests is not None:
        values.setdefault("impersonate", os.getenv("TIKTOK_HTTP_IMPERSONATE", "chrome"))
        values.setdefault("default_headers", False)
        values.setdefault("http_version", "v2")
    values.setdefault("timeout", 30)
    return values


def request(method, url, **kwargs):
    client = _curl_requests if _curl_requests is not None else _requests
    return client.request(method, url, **_kwargs(kwargs))


def get(url, **kwargs):
    return request("GET", url, **kwargs)


def post(url, **kwargs):
    return request("POST", url, **kwargs)

