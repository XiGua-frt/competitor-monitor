"""Reserved TikTok login entry points.

The current API deliberately starts from a browser Cookie header.  These
methods are placeholders for a later, separately captured Passport flow; they
must not silently fall back to guessed requests or fabricate login state.
"""

from __future__ import annotations


class TiktokLoginAPI:
    """Placeholder surface for future TikTok login implementations."""

    @staticmethod
    def _not_ready(flow: str):
        raise NotImplementedError(
            f"TikTok {flow} login is reserved but not implemented; "
            "use TiktokAuth.from_cookie(cookie_str) for the current API"
        )

    def password_login(self, username: str, password: str, **kwargs):
        return self._not_ready("password")

    def qrcode_login(self, **kwargs):
        return self._not_ready("QR-code")

    def send_sms_code(self, phone: str, **kwargs):
        return self._not_ready("SMS code")

    def sms_login(self, phone: str, code: str, **kwargs):
        return self._not_ready("SMS")

    def refresh_session(self, **kwargs):
        return self._not_ready("session refresh")

