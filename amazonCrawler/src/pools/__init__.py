"""Pools layer: IP proxy pool + task leases + session/fingerprint pools + integration (Agent B 交付物)。"""

from .ip_pool import IPPool
from .lease import LeaseManager, LeaseConflictException
from .proxy_source import (
    ApiProxySource,
    EnvProxySource,
    FileProxySource,
    ProxySource,
    create_proxy_source,
    parse_proxy_payload,
    refresh_pool,
)
from .health_checker import ProxyHealthChecker, default_checker
from .cookie_pool import CookieLease, CookiePool, SessionProfile, compute_profile_key
from .fingerprint_pool import FINGERPRINTS, FingerprintPool
from .integration import (
    PoolScopedContext,
    PoolUnavailableError,
    get_cookie_pool,
    get_fingerprint_pool,
    get_ip_pool,
    inject_proxy_to_client,
    pool_scoped_client,
    report_outcome,
    reset_for_tests,
)

__all__ = [
    'IPPool',
    'LeaseManager',
    'LeaseConflictException',
    'ProxySource',
    'EnvProxySource',
    'FileProxySource',
    'ApiProxySource',
    'create_proxy_source',
    'parse_proxy_payload',
    'refresh_pool',
    'ProxyHealthChecker',
    'default_checker',
    'SessionProfile',
    'CookieLease',
    'CookiePool',
    'compute_profile_key',
    'FingerprintPool',
    'FINGERPRINTS',
    'PoolUnavailableError',
    'PoolScopedContext',
    'pool_scoped_client',
    'report_outcome',
    'inject_proxy_to_client',
    'get_ip_pool',
    'get_fingerprint_pool',
    'get_cookie_pool',
    'reset_for_tests',
]
