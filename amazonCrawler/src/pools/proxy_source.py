"""Proxy source providers: env / file / api 代理来源与差集同步。

三类来源（工厂 create_proxy_source 按 AMZ_PROXY_SOURCE 构造）：
- EnvProxySource：环境变量 AMZ_PROXY_LIST，逗号分隔 URL
- FileProxySource：文件每行一个 URL，支持 # 注释；mtime 变化自动重载
- ApiProxySource：GET api_url 返回 JSON/文本 → parse_proxy_payload 解析 URL
  列表（纯函数可单测）；fetch 可注入 fake fetcher，缓存 TTL 默认 300s

合并语义（refresh(pool)）：source 列表与池内已知代理做差集增删——
新增加入、消失的移出（IPPool.update_proxies）。
"""

import json
import os
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, List, Optional
from urllib.parse import urlparse

try:  # pragma: no cover - 取决于 Agent A 是否已就绪
    from src.common.logger import get_logger
except ImportError:
    from ..anti_detection._common_fallback import get_logger

logger = get_logger(__name__)

DEFAULT_API_TTL_SECONDS = 300

_VALID_SCHEMES = ('http://', 'https://', 'socks4://', 'socks5://')


def parse_proxy_payload(payload: str) -> List[str]:
    """把 API 响应正文解析成代理 URL 列表（纯函数，无网络，可单测）。

    支持两种格式：
    - JSON：字符串列表，或含 url/proxy/ip:port 字段的对象列表/对象包裹
    - 纯文本：每行一个 URL（支持 # 注释与空行）
    """
    if not payload or not payload.strip():
        return []
    text = payload.strip()
    # JSON 优先尝试：解析成功即以 JSON 语义为准（即使提取结果为空，
    # 也不回退文本解析——避免把 JSON 碎片误切成伪 URL）
    if text[0] in '[{':
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            data = None
        if data is not None:
            return _extract_from_json(data)
    # 纯文本逐行
    return _parse_lines(text)


def _extract_from_json(data) -> List[str]:
    """从已解析的 JSON 结构递归提取代理 URL 字符串。"""
    urls: List[str] = []

    def _walk(node):
        if isinstance(node, str):
            candidate = node.strip()
            if candidate.lower().startswith(_VALID_SCHEMES):
                urls.append(candidate)
        elif isinstance(node, dict):
            for value in node.values():
                _walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                _walk(item)

    _walk(data)
    return urls


def _parse_lines(text: str) -> List[str]:
    """逐行解析：# 注释与空行跳过，容忍行内 ip:port（补 http:// 前缀）。"""
    urls: List[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        # 去掉可能的 "index ip" 前缀（部分商用 API 格式 "1 http://x"）
        parts = line.split()
        candidate = parts[-1] if len(parts) > 1 else parts[0]
        if '://' not in candidate:
            candidate = 'http://' + candidate
        urls.append(candidate)
    return [u for u in urls if u]


class ProxySource(ABC):
    """代理来源基类：list_proxies() 返回当前全量代理 URL 列表。"""

    @abstractmethod
    def list_proxies(self) -> List[str]:
        """返回当前来源的全部代理 URL（调用方负责与池做差集同步）。"""


class EnvProxySource(ProxySource):
    """环境变量来源：AMZ_PROXY_LIST，逗号分隔代理 URL。"""

    def __init__(self, env_var: str = 'AMZ_PROXY_LIST',
                 env: Optional[dict] = None):
        self.env_var = env_var
        self._env = env if env is not None else os.environ

    def list_proxies(self) -> List[str]:
        raw = self._env.get(self.env_var, '')
        urls = []
        for part in raw.split(','):
            candidate = part.strip()
            if candidate:
                urls.append(candidate)
        return urls


class FileProxySource(ProxySource):
    """文件来源：每行一个 URL，支持 # 注释；mtime 变化自动重载。"""

    def __init__(self, file_path, clock: Callable[[], float] = time.time):
        self.file_path = Path(file_path)
        self._clock = clock
        self._mtime: Optional[float] = None
        self._cached: List[str] = []

    def list_proxies(self) -> List[str]:
        try:
            mtime = self.file_path.stat().st_mtime
        except OSError:
            logger.warning(f"Proxy file not readable: {self.file_path}")
            return []
        # mtime 变化（或首次读取）才重新解析，降低 IO 频率
        if mtime != self._mtime:
            self._mtime = mtime
            self._cached = self._read_file()
            logger.info(
                f"Proxy file (re)loaded: {len(self._cached)} proxies "
                f"from {self.file_path}")
        return list(self._cached)

    def _read_file(self) -> List[str]:
        try:
            text = self.file_path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning(f"Proxy file read failed: {exc}")
            return []
        return _parse_lines(text)


class ApiProxySource(ProxySource):
    """API 来源：GET api_url → parse_proxy_payload 解析；带缓存 TTL。

    fetcher 注入点：默认 _http_fetch 用 curl_cffi；测试注入 fake fetcher，
    绝不发真实网络请求。
    """

    def __init__(self, api_url: str, ttl_seconds: float = DEFAULT_API_TTL_SECONDS,
                 fetcher: Optional[Callable[[str], str]] = None,
                 clock: Callable[[], float] = time.time):
        self.api_url = api_url
        self.ttl_seconds = float(ttl_seconds)
        self._fetcher = fetcher or self._http_fetch
        self._clock = clock
        self._cached: List[str] = []
        self._fetched_at: float = float('-inf')

    def list_proxies(self) -> List[str]:
        now = self._clock()
        if self._cached and (now - self._fetched_at) < self.ttl_seconds:
            return list(self._cached)
        try:
            payload = self._fetcher(self.api_url)
            self._cached = parse_proxy_payload(payload)
            self._fetched_at = now
            logger.info(f"Proxy API fetched: {len(self._cached)} proxies")
        except Exception as exc:
            # 拉取失败：保留旧缓存继续服务（TTL 内重试不发生），下次到期再拉
            logger.warning(f"Proxy API fetch failed, keep stale cache: {exc}")
            if not self._cached:
                self._fetched_at = now  # 失败也计时，避免逐请求重试打爆 API
        return list(self._cached)

    @staticmethod
    def _http_fetch(url: str) -> str:
        """真实网络路径：curl_cffi 轻量 GET（不进单测）。"""
        from curl_cffi import requests
        resp = requests.get(url, timeout=10, impersonate='chrome131')
        resp.raise_for_status()
        return resp.text


def create_proxy_source(config=None, env: Optional[dict] = None
                        ) -> Optional[ProxySource]:
    """按 AMZ_PROXY_SOURCE (env|file|api) 构造 ProxySource。

    Args:
        config: src.common.config.Config（或提供同名字段的任意对象）；None 读环境
        env: 环境变量覆盖字典（测试注入；None 用 os.environ）

    Returns:
        ProxySource 实例；来源未配置/未知时返回 None（调用方回退单代理路径）。
    """
    environ = env if env is not None else os.environ

    def _get(name: str, default: str = '') -> str:
        if config is not None:
            value = getattr(config, name, None)
            if value not in (None, ''):
                return str(value)
        return environ.get(f'AMZ_{name}', environ.get(name, default))

    source_type = _get('PROXY_SOURCE', 'env').strip().lower()
    if source_type == 'env':
        source = EnvProxySource(env=environ)
        if not source.list_proxies():
            return None
        return source
    if source_type == 'file':
        path = _get('PROXY_FILE', 'data/proxies.txt')
        return FileProxySource(path)
    if source_type == 'api':
        api_url = _get('PROXY_API_URL', '')
        if not api_url:
            logger.warning("AMZ_PROXY_SOURCE=api but AMZ_PROXY_API_URL empty")
            return None
        return ApiProxySource(api_url)
    logger.warning(f"Unknown AMZ_PROXY_SOURCE: {source_type}, return None")
    return None


def refresh_pool(pool, source: ProxySource) -> int:
    """来源列表与池做差集同步：新增加入、消失的移出。

    Returns:
        同步后的池大小。
    """
    urls = source.list_proxies()
    pool.update_proxies(urls)
    logger.info(f"Proxy pool synced from source: {len(pool)} proxies")
    return len(pool)
