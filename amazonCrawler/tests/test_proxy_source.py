"""tests for proxy_source — env/file/api 三 provider + mtime 重载 + 工厂。"""

import json

import pytest

from src.pools.proxy_source import (
    ApiProxySource,
    EnvProxySource,
    FileProxySource,
    create_proxy_source,
    parse_proxy_payload,
    refresh_pool,
)
from src.pools.ip_pool import IPPool


class TestParseProxyPayload:
    def test_plain_lines(self):
        text = 'http://1.1.1.1:8080\nhttp://2.2.2.2:8080\n'
        assert parse_proxy_payload(text) == ['http://1.1.1.1:8080',
                                             'http://2.2.2.2:8080']

    def test_comments_and_blank_lines(self):
        text = '# comment\n\nhttp://1.1.1.1:8080\n  # inline\n'
        assert parse_proxy_payload(text) == ['http://1.1.1.1:8080']

    def test_bare_ip_port_gets_scheme(self):
        assert parse_proxy_payload('1.1.1.1:8080') == ['http://1.1.1.1:8080']

    def test_json_string_list(self):
        assert parse_proxy_payload(
            json.dumps(['http://a:1', 'http://b:2'])) == ['http://a:1',
                                                          'http://b:2']

    def test_json_object_wrapped(self):
        payload = json.dumps({'data': [{'proxy': 'http://a:1'},
                                       {'url': 'http://b:2'}]})
        assert parse_proxy_payload(payload) == ['http://a:1', 'http://b:2']

    def test_empty_payload(self):
        assert parse_proxy_payload('') == []
        assert parse_proxy_payload('   \n') == []

    def test_invalid_json_falls_back_to_lines(self):
        text = 'http://a:1\nhttp://b:2'
        assert parse_proxy_payload(text) == ['http://a:1', 'http://b:2']


class TestEnvProxySource:
    def test_comma_separated(self):
        src = EnvProxySource(env={'AMZ_PROXY_LIST': 'http://a:1, http://b:2'})
        assert src.list_proxies() == ['http://a:1', 'http://b:2']

    def test_empty_env(self):
        assert EnvProxySource(env={}).list_proxies() == []


class TestFileProxySource:
    def test_reads_lines_with_comments(self, tmp_path):
        f = tmp_path / 'proxies.txt'
        f.write_text('# pool\nhttp://a:1\nhttp://b:2\n', encoding='utf-8')
        src = FileProxySource(f)
        assert src.list_proxies() == ['http://a:1', 'http://b:2']

    def test_mtime_reload(self, tmp_path):
        f = tmp_path / 'proxies.txt'
        f.write_text('http://a:1\n', encoding='utf-8')
        src = FileProxySource(f)
        assert src.list_proxies() == ['http://a:1']
        # mtime 未变 → 走缓存
        assert src.list_proxies() == ['http://a:1']
        # 修改文件 + 保证 mtime 变化
        f.write_text('http://a:1\nhttp://b:2\n', encoding='utf-8')
        import os
        os.utime(f, (time.time() + 10, time.time() + 10))
        assert src.list_proxies() == ['http://a:1', 'http://b:2']

    def test_missing_file_returns_empty(self, tmp_path):
        src = FileProxySource(tmp_path / 'nope.txt')
        assert src.list_proxies() == []


import time  # noqa: E402  (test_mtime_reload 用)


class TestApiProxySource:
    def test_fetcher_injected_no_network(self):
        calls = []

        def fake_fetcher(url):
            calls.append(url)
            return 'http://a:1\nhttp://b:2'

        src = ApiProxySource('http://api.example/x', ttl_seconds=300,
                             fetcher=fake_fetcher)
        assert src.list_proxies() == ['http://a:1', 'http://b:2']
        assert calls == ['http://api.example/x']

    def test_ttl_cache(self):
        calls = []

        def fake_fetcher(url):
            calls.append(url)
            return 'http://a:1'

        src = ApiProxySource('http://api.example/x', ttl_seconds=300,
                             fetcher=fake_fetcher)
        src.list_proxies()
        src.list_proxies()
        assert len(calls) == 1  # TTL 内不发第二次

    def test_ttl_expiry_refetches(self):
        class FakeClock:
            now = 0.0

            def __call__(self):
                return self.now

        calls = []

        def fake_fetcher(url):
            calls.append(url)
            return 'http://a:1'

        clock = FakeClock()
        src = ApiProxySource('http://api.example/x', ttl_seconds=300,
                             fetcher=fake_fetcher, clock=clock)
        src.list_proxies()
        clock.now += 301
        src.list_proxies()
        assert len(calls) == 2

    def test_fetch_failure_keeps_stale_cache(self):
        state = {'ok': True}

        def fake_fetcher(url):
            if state['ok']:
                return 'http://a:1'
            raise RuntimeError('api down')

        src = ApiProxySource('http://api.example/x', ttl_seconds=300,
                             fetcher=fake_fetcher)
        assert src.list_proxies() == ['http://a:1']
        state['ok'] = False
        assert src.list_proxies() == ['http://a:1']  # 失败回退旧缓存

    def test_json_payload_via_fetcher(self):
        src = ApiProxySource('http://api.example/x', fetcher=lambda u: json.dumps(
            [{'ip': '1.1.1.1', 'port': '8080'}]))
        # ip:port 对象不含 URL 字段 → 文本兜底解析不出 URL，返回空
        assert src.list_proxies() == []


class TestFactory:
    def test_env_source_from_env(self):
        env = {'AMZ_PROXY_SOURCE': 'env',
               'AMZ_PROXY_LIST': 'http://a:1,http://b:2'}
        src = create_proxy_source(env=env)
        assert isinstance(src, EnvProxySource)

    def test_env_source_empty_returns_none(self):
        assert create_proxy_source(env={'AMZ_PROXY_SOURCE': 'env'}) is None

    def test_file_source(self, tmp_path):
        env = {'AMZ_PROXY_SOURCE': 'file',
               'AMZ_PROXY_FILE': str(tmp_path / 'p.txt')}
        src = create_proxy_source(env=env)
        assert isinstance(src, FileProxySource)
        assert src.file_path == tmp_path / 'p.txt'

    def test_api_source(self):
        env = {'AMZ_PROXY_SOURCE': 'api',
               'AMZ_PROXY_API_URL': 'http://api.example/x'}
        src = create_proxy_source(env=env)
        assert isinstance(src, ApiProxySource)

    def test_api_source_missing_url_returns_none(self):
        assert create_proxy_source(
            env={'AMZ_PROXY_SOURCE': 'api'}) is None

    def test_unknown_source_returns_none(self):
        assert create_proxy_source(
            env={'AMZ_PROXY_SOURCE': 'carrier-pigeon'}) is None


class TestRefreshPool:
    def test_diff_sync_into_pool(self):
        pool = IPPool(['http://old:1'])
        src = EnvProxySource(env={'AMZ_PROXY_LIST': 'http://a:1,http://b:2'})
        n = refresh_pool(pool, src)
        assert n == 2
        assert 'http://old:1' not in pool._proxies
