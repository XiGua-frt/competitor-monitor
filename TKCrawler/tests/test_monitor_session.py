"""Tests for scripts/monitor_session.py (login-session health monitor).

Offline only — SessionManager and CurlCffiClient are faked; no real
TikTok request is ever issued.

Coverage:
- check_session(): alive (user_id extracted from the captured passport
  response), dead with JSON body, dead with non-200, session file
  missing, locally expired session
- request discipline: exactly ONE client call per check
- format_status(): plain-text line shape
- append_health_log(): one JSON line with the ts/alive/cookies/user_id/
  error contract
- main(): exit code 0 / 1 branches, stdout contains the relogin
  guidance on dead, health log appended
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import scripts.monitor_session as monitor
from src.anti_detection.session_manager import SessionManager

USER_ID = '7123456789012345678'


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        if self._payload is None:
            raise ValueError('not json')
        return self._payload


class FakeCurlCffiClient:
    """Stands in for CurlCffiClient; records every get() call."""

    instances = []  # lets tests assert call counts across instantiation

    def __init__(self, *args, **kwargs):
        self.calls = []
        FakeCurlCffiClient.instances.append(self)

    # class-level response queue (single response covers the single call)
    response = None

    def get(self, url, **kwargs):
        self.calls.append(url)
        result = type(self).response
        if isinstance(result, Exception):
            raise result
        return result


def make_session(tmp_path, cookies=None, expired=False):
    """A real SessionManager backed by a temp session file written in
    SessionManager's own format (check_session() re-loads from disk)."""
    sm = SessionManager(session_file=tmp_path / 'session.json')
    ck = cookies if cookies is not None else {
        'sessionid': 'x', 'msToken': 'y',
    }
    age = timedelta(hours=25) if expired else timedelta(minutes=10)
    payload = {
        'cookies': ck,
        'last_refresh': (datetime.now() - age).isoformat(),
    }
    sm.session_file.write_text(json.dumps(payload), encoding='utf-8')
    return sm


@pytest.fixture(autouse=True)
def fake_client_env(monkeypatch):
    """Patch CurlCffiClient both in the monitor module and in
    src.anti_detection.curl_client (is_alive imports it lazily from
    there), and reset the call recorder per test."""
    FakeCurlCffiClient.instances = []
    FakeCurlCffiClient.response = FakeResponse(
        200, {'data': {'user_id': USER_ID}})
    monkeypatch.setattr(monitor, 'CurlCffiClient', FakeCurlCffiClient)
    import src.anti_detection.curl_client as curl_module
    monkeypatch.setattr(curl_module, 'CurlCffiClient', FakeCurlCffiClient)
    yield


# ===========================================================================
# 1. check_session()
# ===========================================================================

class TestCheckSession:
    def test_alive_extracts_user_id(self, tmp_path):
        status = monitor.check_session(session_manager=make_session(tmp_path))
        assert status['alive'] is True
        assert status['user_id'] == USER_ID
        assert status['error'] is None
        assert status['cookies'] == 2

    def test_exactly_one_request_per_check(self, tmp_path):
        monitor.check_session(session_manager=make_session(tmp_path))
        assert len(FakeCurlCffiClient.instances) == 1
        assert len(FakeCurlCffiClient.instances[0].calls) == 1

    def test_dead_when_not_logged_in(self, tmp_path, monkeypatch):
        FakeCurlCffiClient.response = FakeResponse(
            200, {'message': 'user not login'})
        status = monitor.check_session(session_manager=make_session(tmp_path))
        assert status['alive'] is False
        assert status['user_id'] is None
        assert 'user not login' in status['error']

    def test_dead_on_non_200(self, tmp_path):
        FakeCurlCffiClient.response = FakeResponse(401, {'message': 'x'})
        status = monitor.check_session(session_manager=make_session(tmp_path))
        assert status['alive'] is False
        assert '401' in status['error']

    def test_dead_on_network_error(self, tmp_path):
        FakeCurlCffiClient.response = ConnectionError('reset by peer')
        status = monitor.check_session(session_manager=make_session(tmp_path))
        assert status['alive'] is False
        assert 'no response captured' in status['error']

    def test_session_file_missing(self, tmp_path):
        sm = SessionManager(session_file=tmp_path / 'nope.json')
        status = monitor.check_session(session_manager=sm)
        assert status['alive'] is False
        assert 'missing or unreadable' in status['error']
        assert status['cookies'] == 0
        assert FakeCurlCffiClient.instances == []  # no request issued

    def test_locally_expired_short_circuits(self, tmp_path):
        status = monitor.check_session(
            session_manager=make_session(tmp_path, expired=True))
        assert status['alive'] is False
        assert 'expired locally' in status['error']
        assert FakeCurlCffiClient.instances == []  # no request issued

    def test_user_id_none_when_payload_lacks_it(self, tmp_path):
        FakeCurlCffiClient.response = FakeResponse(200, {'data': {}})
        status = monitor.check_session(session_manager=make_session(tmp_path))
        assert status['alive'] is False
        assert status['user_id'] is None


# ===========================================================================
# 2. Output formatting + log contract
# ===========================================================================

class TestOutputAndLog:
    def test_format_status_alive_line(self, tmp_path):
        status = monitor.check_session(session_manager=make_session(tmp_path))
        line = monitor.format_status(status)
        assert 'ts=' in line
        assert 'cookies=2' in line
        assert 'is_alive=True' in line
        assert f'user_id={USER_ID}' in line
        assert 'error=' not in line

    def test_format_status_dead_includes_error(self, tmp_path):
        FakeCurlCffiClient.response = FakeResponse(401, {})
        status = monitor.check_session(session_manager=make_session(tmp_path))
        line = monitor.format_status(status)
        assert 'is_alive=False' in line
        assert 'error=' in line

    def test_append_health_log_json_line(self, tmp_path, monkeypatch):
        log_file = tmp_path / 'session_health.log'
        monkeypatch.setattr(monitor, 'HEALTH_LOG', log_file)
        status = {'ts': '2026-10-08T00:00:00Z', 'alive': True,
                  'cookies': 27, 'user_id': USER_ID, 'error': None}
        monitor.append_health_log(status)

        lines = log_file.read_text(encoding='utf-8').strip().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed == status

    def test_append_health_log_appends_not_overwrites(
            self, tmp_path, monkeypatch):
        log_file = tmp_path / 'session_health.log'
        monkeypatch.setattr(monitor, 'HEALTH_LOG', log_file)
        for ts in ('2026-10-08T00:00:00Z', '2026-10-08T01:00:00Z'):
            monitor.append_health_log(
                {'ts': ts, 'alive': False, 'cookies': 0,
                 'user_id': None, 'error': 'dead'})
        lines = log_file.read_text(encoding='utf-8').strip().splitlines()
        assert len(lines) == 2
        assert [json.loads(l)['ts'] for l in lines] == [
            '2026-10-08T00:00:00Z', '2026-10-08T01:00:00Z']


# ===========================================================================
# 3. main(): exit codes + relogin guidance + log write
# ===========================================================================

class TestMain:
    @pytest.fixture
    def run_main(self, tmp_path, monkeypatch):
        """Prepare main() with an injectable session + temp health log."""
        holder = {'session': None, 'log': tmp_path / 'session_health.log'}
        monkeypatch.setattr(monitor, 'HEALTH_LOG', holder['log'])

        def _run(session):
            holder['session'] = session
            monkeypatch.setattr(
                monitor, 'SessionManager', lambda: session)
            return monitor.main()

        return _run, holder

    def test_exit_zero_when_alive(self, tmp_path, run_main, capsys):
        run, holder = run_main
        code = run(make_session(tmp_path))
        assert code == 0
        out = capsys.readouterr().out
        assert 'is_alive=True' in out
        parsed = json.loads(holder['log'].read_text(
            encoding='utf-8').strip())
        assert parsed['alive'] is True

    def test_exit_one_and_relogin_guidance_when_dead(
            self, tmp_path, run_main, capsys):
        FakeCurlCffiClient.response = FakeResponse(401, {})
        run, holder = run_main
        code = run(make_session(tmp_path))
        assert code == 1
        out = capsys.readouterr().out
        assert 'is_alive=False' in out
        # full copyable relogin command present
        assert 'login_tiktok.py' in out
        assert 'scripts/login_tiktok.py' in out
        parsed = json.loads(holder['log'].read_text(
            encoding='utf-8').strip())
        assert parsed['alive'] is False

    def test_exit_one_when_session_file_missing(self, tmp_path, run_main,
                                                capsys):
        sm = SessionManager(session_file=tmp_path / 'nope.json')
        run, _ = run_main
        assert run(sm) == 1
        out = capsys.readouterr().out
        assert 'is_alive=False' in out
        assert 'login_tiktok.py' in out
