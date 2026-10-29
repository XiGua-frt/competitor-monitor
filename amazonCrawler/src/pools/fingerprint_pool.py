"""Fingerprint pool: curl_cffi impersonate 指纹轮换与封禁冷却。

指纹列表为 curl_cffi 0.16.3 实测支持的 BrowserTypeLiteral 值
（curl_cffi.requests.impersonate.BrowserTypeLiteral get_args 确认）。
某指纹被 503 时 report_ban 冷却 1h，acquire 换下一个；
strategy='fixed'（默认）恒返回 chrome131，向后兼容。
"""

import time
from typing import Callable, Dict, List, Optional

try:  # pragma: no cover - 取决于 Agent A 是否已就绪
    from src.common.logger import get_logger
except ImportError:
    from ..anti_detection._common_fallback import get_logger

logger = get_logger(__name__)

# curl_cffi 0.16.3 实测支持（BrowserTypeLiteral），覆盖主流桌面浏览器版本
FINGERPRINTS: List[str] = [
    'chrome131',
    'chrome124',
    'chrome120',
    'chrome116',
    'edge101',
    'safari17_0',
]

DEFAULT_FINGERPRINT = 'chrome131'
BAN_COOLDOWN_SECONDS = 60 * 60  # 指纹被 ban 冷却 1 小时


class FingerprintPool:
    """curl_cffi impersonate 指纹池：rotate 轮询 / fixed 固定 + ban 冷却。"""

    def __init__(self, fingerprints: Optional[List[str]] = None,
                 strategy: str = 'fixed',
                 ban_cooldown: float = BAN_COOLDOWN_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        """Initialize fingerprint pool.

        Args:
            fingerprints: 可用指纹列表（默认 FINGERPRINTS）
            strategy: 'fixed'（默认，恒返回 DEFAULT_FINGERPRINT，向后兼容）
                | 'rotate'（轮询 + ban 冷却跳过）
            ban_cooldown: 指纹 ban 冷却时长（默认 1 小时）
            clock: 时间函数（测试注入假时钟）
        """
        self.fingerprints = (list(fingerprints) if fingerprints is not None
                             else list(FINGERPRINTS))
        if not self.fingerprints:
            raise ValueError('FingerprintPool requires at least one fingerprint')
        self.strategy = strategy
        self.ban_cooldown = float(ban_cooldown)
        self._clock = clock
        self._rr_index = 0
        # fingerprint -> 冷却到期时刻
        self._cooldown_until: Dict[str, float] = {}

    def acquire(self) -> str:
        """取下一个可用指纹；rotate 模式冷却中的跳过，全冷却回退默认指纹。

        fixed 模式恒返回 DEFAULT_FINGERPRINT（与现有 CurlCffiClient 默认一致）。
        """
        if self.strategy != 'rotate':
            return DEFAULT_FINGERPRINT

        now = self._clock()
        n = len(self.fingerprints)
        for _ in range(n):
            fp = self.fingerprints[self._rr_index % n]
            self._rr_index = (self._rr_index + 1) % n
            until = self._cooldown_until.get(fp)
            if until is not None and until > now:
                continue
            self._cooldown_until.pop(fp, None)
            return fp

        # 全部冷却中：回退默认指纹（不可用指纹劣于无指纹——TLS 指纹缺失更可疑）
        logger.warning('All fingerprints in cooldown, fallback to default')
        return DEFAULT_FINGERPRINT

    def report_ban(self, fingerprint: str) -> None:
        """指纹被 503/challenge 标记，冷却 1 小时（滑动重置）。"""
        if fingerprint not in self.fingerprints:
            logger.warning(f"report_ban for unknown fingerprint: {fingerprint}")
            return
        self._cooldown_until[fingerprint] = self._clock() + self.ban_cooldown
        logger.warning(
            f"Fingerprint banned for {self.ban_cooldown:.0f}s: {fingerprint}")

    def report_success(self, fingerprint: str) -> None:
        """成功响应清除 ban 冷却（主动恢复路径）。"""
        self._cooldown_until.pop(fingerprint, None)

    def healthy_count(self) -> int:
        """当前不在冷却中的指纹数量。"""
        now = self._clock()
        return sum(
            1 for fp in self.fingerprints
            if self._cooldown_until.get(fp, float('-inf')) <= now)
