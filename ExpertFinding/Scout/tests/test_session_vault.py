"""Task 5：会话边界（导入/撤销/到期/健康）与 Signer 后备接口（none-v1）。

约束：
- 全程离线：健康检查通过注入 fake probe 完成，不发起任何真实网络调用；
- 所有 fixture 均为合成数据，不读取 .secrets/ 下的真实会话；
- 本文件独占 tests/test_session_vault.py，不创建 tests/test_transport_policy.py，
  也不依赖 tests/conftest.py（需要的 fixture 全部定义在此文件内）。
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from app.sessions.importers import (
    SessionImportError,
    import_session,
    session_id_hash,
)
from app.sessions.models import (
    SessionContext,
    SessionHealthStatus,
    SessionMeta,
    SessionStatus,
)
from app.sessions.revoke import (
    SessionExpiredError,
    SessionNotFoundError,
    SessionRevokedError,
    assert_transport_allowed,
    is_active,
)
from app.sessions.vault import InMemorySessionVault, check_health
from app.signing.base import (
    ExternalSignatureRejected,
    ReadonlyRequest,
    SignedRequest,
    Signer,
    SignerError,
    UrlNotAllowlisted,
    WritePathRejected,
)
from app.signing.versions import NoopSigner, SEARCH_READONLY_URL

# --------------------------------------------------------------------------
# 合成会话材料（非真实凭证，仅用于测试）
# --------------------------------------------------------------------------

SEARCH_URL = "https://www.tiktok.com/api/search/general/full/"

SYNTHETIC_COOKIES = {
    "sessionid": "SYNTHETIC-NOT-A-REAL-SESSION-0001",
    "sessionid_ss": "SYNTHETIC-NOT-A-REAL-SESSION-SS-0001",
    "sid_tt": "SYNTHETIC-NOT-A-REAL-SIDTT-0001",
    "msToken": "SYNTHETIC-NOT-A-REAL-MSTOKEN-0001",
    "ttwid": "SYNTHETIC-NOT-A-REAL-TTWID-0001",
    "odin_tt": "SYNTHETIC-NOT-A-REAL-ODINTT-0001",
}

SECRET_TOKENS = ("SYNTHETIC-NOT-A-REAL-SESSION-0001",
                 "SYNTHETIC-NOT-A-REAL-MSTOKEN-0001")


def _write_session(path, cookies=None, extracted_at=None, profile="Chrome/Default"):
    payload = {
        "extracted_at": extracted_at or "2026-10-02T00:00:00+00:00",
        "profile": profile,
        "cookies": dict(SYNTHETIC_COOKIES if cookies is None else cookies),
    }
    with open(path, "w") as f:
        json.dump(payload, f)
    return str(path)


@pytest.fixture
def tmp_dir():
    """自建临时目录：不依赖 pytest 的 tmp_path（多人并行时 TMPDIR 根会冲突）。"""
    with tempfile.TemporaryDirectory(prefix="scout-session-test-") as d:
        yield pathlib.Path(d)


@pytest.fixture
def session_file(tmp_dir):
    """合成的 tiktok_session.json（结构对齐 tools/extract_tiktok_session.py 输出）。"""
    return _write_session(tmp_dir / "tiktok_session.json")


@pytest.fixture
def fake_session(session_file):
    return import_session(session_file)


@pytest.fixture
def session_context(fake_session):
    return SessionContext.from_meta(fake_session)


class FakeHealthProbe:
    """替代 TikTokWebClient.health()，记录调用次数以验证"绝不重试"。"""

    def __init__(self, result=None, exc=None):
        self.result = result if result is not None else {"ok": True, "http": 200}
        self.exc = exc
        self.calls = 0

    def health(self):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.result


# ==========================================================================
# Step 1：会话导入与敏感值边界
# ==========================================================================

def test_import_returns_metadata_and_never_exposes_cookie_values(session_file):
    session = import_session(session_file)
    public = session.public_status()
    assert public["status"] == "active"
    assert "sessionid" not in repr(public)
    assert "msToken" not in repr(public)


def test_import_metadata_carries_no_plaintext_secret(session_file):
    session = import_session(session_file)
    blob = repr(session) + repr(session.public_status()) + json.dumps(
        session.public_status(), ensure_ascii=False)
    for token in SECRET_TOKENS:
        assert token not in blob
    assert "sessionid" not in blob
    assert "msToken" not in blob


def test_session_id_hash_is_irreversible_and_stable(session_file, tmp_dir):
    first = import_session(session_file)
    assert len(first.session_id_hash) == 64
    assert first.session_id_hash != SYNTHETIC_COOKIES["sessionid"]
    assert SYNTHETIC_COOKIES["sessionid"] not in first.session_id_hash

    other = _write_session(
        tmp_dir / "other_session.json",
        {**SYNTHETIC_COOKIES, "sessionid": "SYNTHETIC-NOT-A-REAL-SESSION-0002"})
    second = import_session(other)
    assert second.session_id_hash != first.session_id_hash
    # 同一份材料重复导入得到稳定 hash
    assert import_session(session_file).session_id_hash == first.session_id_hash
    assert session_id_hash(SYNTHETIC_COOKIES["sessionid"]) == first.session_id_hash


def test_import_rejects_missing_login_cookie(tmp_dir):
    broken = _write_session(
        tmp_dir / "no_login.json",
        {k: v for k, v in SYNTHETIC_COOKIES.items()
         if k not in ("sessionid", "sessionid_ss", "sid_tt")})
    with pytest.raises(SessionImportError):
        import_session(broken)


def test_import_rejects_missing_ms_token(tmp_dir):
    broken = _write_session(
        tmp_dir / "no_mstoken.json",
        {k: v for k, v in SYNTHETIC_COOKIES.items() if k != "msToken"})
    with pytest.raises(SessionImportError):
        import_session(broken)


def test_import_rejects_unreadable_or_malformed_file(tmp_dir):
    with pytest.raises(SessionImportError):
        import_session(str(tmp_dir / "missing.json"))
    bad = tmp_dir / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(SessionImportError):
        import_session(str(bad))


def test_import_records_expiry_when_source_provides_it(tmp_dir):
    expires = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    path = tmp_dir / "with_expiry.json"
    with open(path, "w") as f:
        json.dump({
            "extracted_at": "2026-10-02T00:00:00+00:00",
            "profile": "Chrome/Default",
            "cookies": dict(SYNTHETIC_COOKIES),
            "expires": {"sessionid": expires},
        }, f)
    meta = import_session(str(path))
    assert meta.expires_at == expires


# ==========================================================================
# Step 1：撤销 / 到期 阻断 transport（在发起网络之前）
# ==========================================================================

def test_revoked_session_blocks_transport_before_network(fake_session):
    vault = InMemorySessionVault(fake_session)
    vault.revoke(fake_session.session_id)
    with pytest.raises(SessionRevokedError):
        vault.require_active(fake_session.session_id)


def test_revoked_session_never_hands_out_cookie_material(fake_session):
    vault = InMemorySessionVault(fake_session)
    vault.store_material(fake_session.session_id, SYNTHETIC_COOKIES)
    vault.revoke(fake_session.session_id)
    with pytest.raises(SessionRevokedError):
        vault.material(fake_session.session_id)
    assert "sessionid" not in repr(vault)
    for token in SECRET_TOKENS:
        assert token not in repr(vault)


def test_revoke_gate_stops_before_any_transport_call(fake_session):
    calls: list[str] = []

    class NetworkMustNotRun:
        def fetch(self, url, **_kwargs):
            calls.append(url)
            raise AssertionError("transport must not be reached")

    vault = InMemorySessionVault(fake_session)
    vault.revoke(fake_session.session_id)
    meta = vault.get(fake_session.session_id)
    transport = NetworkMustNotRun()
    with pytest.raises(SessionRevokedError):
        assert_transport_allowed(meta)  # 先于网络调用抛出
        transport.fetch(SEARCH_URL)
    assert calls == []


def test_expired_session_blocks_transport(fake_session):
    expired = SessionMeta(
        session_id=fake_session.session_id,
        session_id_hash=fake_session.session_id_hash,
        source=fake_session.source,
        status=SessionStatus.ACTIVE,
        imported_at=fake_session.imported_at,
        expires_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
    )
    assert is_active(expired) is False
    with pytest.raises(SessionExpiredError):
        assert_transport_allowed(expired)


def test_active_session_allows_transport(fake_session):
    assert is_active(fake_session) is True
    assert_transport_allowed(fake_session)


def test_unknown_session_is_not_found(fake_session):
    vault = InMemorySessionVault(fake_session)
    with pytest.raises(SessionNotFoundError):
        vault.require_active("does-not-exist")


def test_vault_never_writes_plaintext_to_disk(fake_session, tmp_dir, session_file):
    before = set(os.listdir(tmp_dir))
    vault = InMemorySessionVault(fake_session)
    vault.store_material(fake_session.session_id, SYNTHETIC_COOKIES)
    vault.revoke(fake_session.session_id)
    vault.checkpoint()  # 内存快照：只含元数据
    assert set(os.listdir(tmp_dir)) == before == {os.path.basename(session_file)}


def test_vault_checkpoint_contains_no_secrets(fake_session):
    vault = InMemorySessionVault(fake_session)
    vault.store_material(fake_session.session_id, SYNTHETIC_COOKIES)
    snapshot = json.dumps(vault.checkpoint(), ensure_ascii=False)
    for token in SECRET_TOKENS:
        assert token not in snapshot
    assert "sessionid" not in snapshot
    assert "msToken" not in snapshot


# ==========================================================================
# Step 3：健康检查（注入 fake，遇到 WAF 只返回停止状态，不重试）
# ==========================================================================

def test_health_ok(fake_session):
    probe = FakeHealthProbe({"ok": True, "http": 200})
    status = check_health(fake_session, probe)
    assert isinstance(status, SessionHealthStatus)
    assert status.ok is True and status.stopped is False
    assert probe.calls == 1


def test_health_waf_returns_stop_without_retry(fake_session):
    probe = FakeHealthProbe(
        {"ok": False, "error": "WAF 挑战页：出口 IP 或指纹被标记。停止任务。"})
    status = check_health(fake_session, probe)
    assert status.ok is False
    assert status.stopped is True
    assert status.status == "waf_challenge"
    assert probe.calls == 1  # 只探测一次，绝不重试


def test_health_waf_status_channel_returns_stop_without_retry(fake_session):
    """真实 TikTokWebClient.health() 的 WAF 形状：只有 status 通道，不含 error/detail。"""
    probe = FakeHealthProbe({"ok": False, "http": 200, "status": "waf_challenge"})
    status = check_health(fake_session, probe)
    assert status.ok is False
    assert status.stopped is True
    assert status.status == "waf_challenge"
    assert probe.calls == 1  # 只探测一次，绝不重试


def test_health_unhealthy_is_not_waf(fake_session):
    status = check_health(fake_session, FakeHealthProbe({"ok": False, "http": 403}))
    assert status.stopped is True
    assert status.status == "unhealthy"


def test_health_skips_network_for_revoked_session(fake_session):
    vault = InMemorySessionVault(fake_session)
    vault.revoke(fake_session.session_id)
    probe = FakeHealthProbe({"ok": True, "http": 200})
    status = check_health(vault.get(fake_session.session_id), probe)
    assert status.status == "revoked"
    assert status.stopped is True
    assert probe.calls == 0


# ==========================================================================
# Step 2：Signer Protocol 与 none-v1 后备实现
# ==========================================================================

def test_noop_signer_satisfies_protocol():
    signer = NoopSigner()
    assert isinstance(signer, Signer)
    assert signer.version == "none-v1"


def test_noop_signer_passes_readonly_request_through(session_context):
    signer = NoopSigner()
    request = ReadonlyRequest(url=SEARCH_URL,
                              params={"keyword": "fitness coach", "offset": "0"},
                              headers={"Accept": "application/json"})
    signed = signer.sign_readonly(request, session_context)
    assert isinstance(signed, SignedRequest)
    assert signed.url == SEARCH_URL
    assert signed.params["keyword"] == "fitness coach"
    assert signed.headers["Accept"] == "application/json"
    assert signed.signer_version == "none-v1"


def test_noop_signer_default_allowlist_matches_verified_search_endpoint():
    assert SEARCH_READONLY_URL == SEARCH_URL
    NoopSigner()  # 默认允许已验证无需 X-Bogus 的只读端点


def test_signer_rejects_arbitrary_url(session_context):
    signer = NoopSigner()
    for url in ("https://www.tiktok.com/api/user/detail/",
                "https://evil.example.com/api/search/general/full/",
                "http://www.tiktok.com/api/search/general/full/",
                "file:///etc/passwd"):
        with pytest.raises(SignerError):
            signer.sign_readonly(ReadonlyRequest(url=url), session_context)


def test_signer_rejects_arbitrary_url_raises_not_allowlisted(session_context):
    with pytest.raises(UrlNotAllowlisted):
        NoopSigner().sign_readonly(
            ReadonlyRequest(url="https://www.tiktok.com/api/user/detail/"),
            session_context)


def test_signer_rejects_write_method(session_context):
    with pytest.raises(WritePathRejected):
        NoopSigner().sign_readonly(
            ReadonlyRequest(url=SEARCH_URL, method="POST"), session_context)


def test_signer_rejects_write_path_in_allowlist():
    with pytest.raises(WritePathRejected):
        NoopSigner(allowed_urls=("https://www.tiktok.com/api/comment/publish/",))
    with pytest.raises(WritePathRejected):
        NoopSigner(allowed_urls=(SEARCH_URL,), allowed_methods=("GET", "POST"))


def test_signer_rejects_external_signature_fields(session_context):
    signer = NoopSigner()
    with pytest.raises(ExternalSignatureRejected):
        signer.sign_readonly(
            ReadonlyRequest(url=SEARCH_URL, params={"X-Bogus": "deadbeef"}),
            session_context)
    with pytest.raises(ExternalSignatureRejected):
        signer.sign_readonly(
            ReadonlyRequest(url=SEARCH_URL, headers={"x-bogus": "deadbeef"}),
            session_context)
    with pytest.raises(ExternalSignatureRejected):
        signer.sign_readonly(
            ReadonlyRequest(url=SEARCH_URL, params={"_signature": "abc"}),
            session_context)
    with pytest.raises(ExternalSignatureRejected):
        signer.sign_readonly(
            ReadonlyRequest(url=SEARCH_URL + "?X-Bogus=deadbeef"), session_context)


@pytest.mark.parametrize("field", [
    # X-Bogus 家族：大小写 / 连字符 / 下划线 / 驼峰
    "X-Bogus", "x-bogus", "XBogus", "x_bogus", "X_BOGUS",
    # verifyFp：TikTok query 的真实驼峰拼写
    "verifyFp", "verify_fp", "VerifyFp", "verifyfp", "VERIFY_FP",
    # msToken
    "msToken", "ms_token", "mstoken", "MSTOKEN",
    # A-Bogus 与其它签名头
    "aBogus", "a_bogus", "ABogus", "X-Gnarly", "x_tt_params", "_signature",
])
def test_signer_rejects_signature_field_spelling_variants(session_context, field):
    """签名字段拦截必须覆盖大小写/驼峰/下划线变体，不能只认标准小写拼写。"""
    signer = NoopSigner()
    # params
    with pytest.raises(ExternalSignatureRejected):
        signer.sign_readonly(
            ReadonlyRequest(url=SEARCH_URL, params={field: "deadbeef"}),
            session_context)
    # headers
    with pytest.raises(ExternalSignatureRejected):
        signer.sign_readonly(
            ReadonlyRequest(url=SEARCH_URL, headers={field: "deadbeef"}),
            session_context)
    # URL query（真实请求里 verifyFp / X-Bogus 就出现在这里）
    with pytest.raises(ExternalSignatureRejected):
        signer.sign_readonly(
            ReadonlyRequest(url=f"{SEARCH_URL}?{field}=deadbeef"), session_context)


def test_signer_requires_explicit_session(session_context):
    """签名必须显式带会话：不允许 None / 缺省的会话参与签名。"""
    with pytest.raises(TypeError):
        NoopSigner().sign_readonly(ReadonlyRequest(url=SEARCH_URL))


def test_signer_refuses_revoked_session(fake_session):
    revoked = SessionContext.from_meta(
        fake_session.with_status(SessionStatus.REVOKED))
    with pytest.raises(SignerError):
        NoopSigner().sign_readonly(ReadonlyRequest(url=SEARCH_URL), revoked)


def test_signed_request_never_carries_session_secrets(session_context):
    signed = NoopSigner().sign_readonly(
        ReadonlyRequest(url=SEARCH_URL, params={"keyword": "x"}), session_context)
    for token in SECRET_TOKENS:
        assert token not in repr(signed)
    assert "sessionid" not in repr(signed)
    assert "msToken" not in repr(signed)
