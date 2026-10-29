"""Browser-shaped TikTok headers.

The values below come from the Chrome 153 capture in the shared browser.  The
builder deliberately does not add arbitrary ``cache-control`` or legacy
Chrome-132 fields: extra fields are just as much a wire mismatch as missing
fields.
"""

from __future__ import annotations

from collections import OrderedDict
from enum import Enum

from .auth import TiktokAuth


class HeaderType(Enum):
    DOC = "DOC"
    GET = "GET"
    POST = "POST"
    FORM = "FORM"


class Header:
    def __init__(self, values=None):
        self.headers = OrderedDict(values or ())

    def set_header(self, key, value):
        if value is not None:
            self.headers[str(key)] = str(value)
        return self

    def remove_header(self, key):
        self.headers.pop(key, None)
        return self

    def get(self):
        return self.headers

    def __call__(self):
        return self.headers


class HeaderBuilder:
    """Build only fields observed for the selected browser request class."""

    @staticmethod
    def build(header_type: HeaderType, auth: TiktokAuth, *, referer: str,
              origin: str = "", content_length: str | None = None,
              sec_fetch_site: str = "same-origin") -> Header:
        h = Header()
        if header_type == HeaderType.DOC:
            h.set_header("accept", "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7")
            h.set_header("accept-language", auth.accept_language)
            h.set_header("cache-control", "no-cache")
            h.set_header("pragma", "no-cache")
            h.set_header("priority", "u=0, i")
            h.set_header("referer", referer)
            h.set_header("sec-ch-ua", auth.sec_ch_ua)
            h.set_header("sec-ch-ua-mobile", "?0")
            h.set_header("sec-ch-ua-platform", auth.sec_ch_ua_platform)
            h.set_header("sec-fetch-dest", "document")
            h.set_header("sec-fetch-mode", "navigate")
            h.set_header("sec-fetch-site", "same-origin")
            h.set_header("sec-fetch-user", "?1")
            h.set_header("upgrade-insecure-requests", "1")
            h.set_header("user-agent", auth.user_agent)
            h.set_header("cookie", auth.cookie_str)
            return h

        # Chrome ExtraInfo order for the captured same-origin XHRs.
        h.set_header("sec-ch-ua-platform", auth.sec_ch_ua_platform)
        h.set_header("referer", referer)
        h.set_header("user-agent", auth.user_agent)
        h.set_header("sec-ch-ua", auth.sec_ch_ua)
        h.set_header("sec-ch-ua-mobile", "?0")
        if header_type == HeaderType.POST:
            h.set_header("content-type", "application/json")
        elif header_type == HeaderType.FORM:
            h.set_header("content-type", "application/x-www-form-urlencoded")
        h.set_header("accept", "*/*")
        h.set_header("accept-encoding", "gzip, deflate, br, zstd")
        h.set_header("accept-language", auth.accept_language)
        h.set_header("cookie", auth.cookie_str)
        if origin:
            h.set_header("origin", origin)
        if content_length is not None:
            h.set_header("content-length", content_length)
        h.set_header("priority", "u=1, i")
        h.set_header("sec-fetch-dest", "empty")
        h.set_header("sec-fetch-mode", "cors")
        h.set_header("sec-fetch-site", sec_fetch_site)
        return h
