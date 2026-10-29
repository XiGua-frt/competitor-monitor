"""Simple IP proxy pool: round-robin acquire + failure cooldown (30 min).

契约：IPPool(list[str] 代理 URL 轮询) + acquire() / report_failure(url) /
healthy_count()。纯内存实现，无 Redis/网络依赖，离线单测友好。

v2 增强（向后兼容：原四个方法签名与语义不变）：
- 每代理统计：success/fail 计数、滑动窗口成功率、平均响应时间、last_used_at
- 动态降权：连续失败 >=3 次冷却时长按 30min -> 2h -> 12h 递进
- strategy='weighted'：按滑动窗口成功率加权随机选择（低分代理大概率被跳过）
- stats()：供 HealthService/观测用，代理 URL 脱敏
"""

import random
import re
import time
from collections import deque
from typing import Callable, Deque, Dict, List, Optional

try:  # pragma: no cover - 取决于 Agent A 是否已就绪
    from src.common.logger import get_logger
except ImportError:
    from ..anti_detection._common_fallback import get_logger

logger = get_logger(__name__)

# 失败冷却时长（秒）：标记失败后 30 分钟内不参与轮询
FAILURE_COOLDOWN_SECONDS = 30 * 60

# 动态降权阶梯：连续失败次数 -> 冷却倍率
COOLDOWN_ESCALATION = (
    (3, 4),    # 连续 3 次失败：30min -> 2h
    (6, 24),   # 连续 6 次失败：-> 12h
)

# 滑动窗口长度：成功率只看最近 N 次结果
SLIDING_WINDOW_SIZE = 20


class _ProxyStats:
    """单个代理的运行统计（内存态，不持久化）。"""

    __slots__ = ('success_count', 'fail_count', 'continuous_fail_count',
                 'window', 'response_times', 'last_used_at', 'last_error_at')

    def __init__(self, window_size: int = SLIDING_WINDOW_SIZE):
        self.success_count: int = 0
        self.fail_count: int = 0
        self.continuous_fail_count: int = 0
        # 滑动窗口：最近 window_size 次 1(成功)/0(失败)
        self.window: Deque[int] = deque(maxlen=window_size)
        self.response_times: Deque[float] = deque(maxlen=window_size)
        self.last_used_at: Optional[float] = None
        self.last_error_at: Optional[float] = None

    def record_success(self, response_time: Optional[float],
                       now: float) -> None:
        self.success_count += 1
        self.continuous_fail_count = 0
        self.window.append(1)
        if response_time is not None:
            self.response_times.append(float(response_time))
        self.last_used_at = now

    def record_failure(self, now: float) -> None:
        self.fail_count += 1
        self.continuous_fail_count += 1
        self.window.append(0)
        self.last_used_at = now
        self.last_error_at = now

    @property
    def success_rate(self) -> float:
        """滑动窗口成功率；无样本视为 1.0（不因冷启动被加权惩罚）。"""
        if not self.window:
            return 1.0
        return sum(self.window) / len(self.window)

    @property
    def avg_response_time(self) -> Optional[float]:
        if not self.response_times:
            return None
        return sum(self.response_times) / len(self.response_times)


class IPPool:
    """代理 URL 轮询池，失败代理进入冷却（30min → 2h → 12h 动态降权）。"""

    def __init__(
        self,
        proxy_urls: Optional[List[str]] = None,
        cooldown_seconds: float = FAILURE_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        strategy: str = 'round_robin',
        window_size: int = SLIDING_WINDOW_SIZE,
        rng: Optional[random.Random] = None,
    ):
        """Initialize IP pool.

        Args:
            proxy_urls: 代理 URL 列表（http://host:port 或 http://user:pass@host:port）
            cooldown_seconds: 基础失败冷却时长，默认 30 分钟（可注入缩短便于测试）
            clock: 时间函数（默认 time.monotonic；测试可注入假时钟）
            strategy: 默认选择策略 'round_robin' | 'weighted'（acquire 可逐次覆盖）
            window_size: 成功率滑动窗口长度
            rng: 随机源（weighted 策略用；测试可注入可控 rng）
        """
        self.cooldown_seconds = float(cooldown_seconds)
        self.strategy = strategy
        self.window_size = int(window_size)
        self._clock = clock
        self._rng = rng or random.Random()
        self._proxies: List[str] = list(proxy_urls or [])
        # url -> 冷却到期时刻（clock 时间戳）；无条目 = 健康
        self._cooldown_until: Dict[str, float] = {}
        self._rr_index = 0
        self._stats: Dict[str, _ProxyStats] = {
            p: _ProxyStats(window_size) for p in self._proxies
        }

    # ------------------------------------------------------------------ API

    def acquire(self, strategy: Optional[str] = None) -> Optional[str]:
        """取下一个健康代理；全部冷却中返回 None。

        Args:
            strategy: None 用构造时的默认策略；'round_robin' 轮询（默认行为，
                与 v1 语义完全一致）；'weighted' 按滑动窗口成功率加权随机，
                低成功率代理大概率被跳过但不会被绝对排除（未冷却仍可被选中）。
        """
        if not self._proxies:
            logger.warning("IP pool is empty, acquire returns None")
            return None

        chosen = (strategy or self.strategy or 'round_robin')
        if chosen == 'weighted':
            return self._acquire_weighted()
        return self._acquire_round_robin()

    def report_failure(self, proxy_url: str,
                       response_time: Optional[float] = None) -> None:
        """标记代理失败，进入冷却（连续失败触发动态降权阶梯）。

        冷却是滑动重置：重复 report_failure 会重新计时。
        response_time 为可选参数，失败时也纳入平均响应时间统计。
        """
        stats = self._stats.get(proxy_url)
        if stats is None:
            logger.warning(
                f"report_failure for unknown proxy: {self._mask(proxy_url)}"
            )
            return
        now = self._clock()
        stats.record_failure(now)
        if response_time is not None:
            stats.response_times.append(float(response_time))

        cooldown = self._escalated_cooldown(stats.continuous_fail_count)
        self._cooldown_until[proxy_url] = now + cooldown
        logger.warning(
            f"Proxy marked failed (continuous={stats.continuous_fail_count}), "
            f"cooldown {cooldown:.0f}s: {self._mask(proxy_url)}"
        )

    def report_success(self, proxy_url: str,
                       response_time: Optional[float] = None) -> None:
        """成功响应：清除冷却标记、重置连续失败计数、记录响应时间。"""
        stats = self._stats.get(proxy_url)
        if stats is None:
            logger.warning(
                f"report_success for unknown proxy: {self._mask(proxy_url)}"
            )
            return
        stats.record_success(response_time, self._clock())
        self._cooldown_until.pop(proxy_url, None)

    def healthy_count(self) -> int:
        """当前不在冷却中的代理数量。"""
        now = self._clock()
        return sum(
            1
            for p in self._proxies
            if self._cooldown_until.get(p, float('-inf')) <= now
        )

    def stats(self) -> List[Dict]:
        """全部代理的统计快照（代理 URL 脱敏，供观测/HealthService 用）。"""
        now = self._clock()
        out = []
        for p in self._proxies:
            s = self._stats[p]
            until = self._cooldown_until.get(p)
            out.append({
                'proxy': self._mask(p),
                'success_count': s.success_count,
                'fail_count': s.fail_count,
                'success_rate': round(s.success_rate, 4),
                'avg_response_time': (
                    round(s.avg_response_time, 4)
                    if s.avg_response_time is not None else None
                ),
                'last_used_at': s.last_used_at,
                'continuous_fail_count': s.continuous_fail_count,
                'in_cooldown': bool(until is not None and until > now),
            })
        return out

    def update_proxies(self, proxy_urls: List[str]) -> None:
        """与来源列表做差集增删（新增加入、消失的移出），供 ProxySource 同步。

        保持相对顺序：先追加新代理，再移除已消失的；轮询游标收敛到合法范围。
        """
        new_list = list(proxy_urls)
        known = set(self._proxies)
        incoming = set(new_list)
        for p in new_list:
            if p not in known:
                self._proxies.append(p)
                self._stats[p] = _ProxyStats(self.window_size)
        for p in list(self._proxies):
            if p not in incoming:
                self._proxies.remove(p)
                self._stats.pop(p, None)
                self._cooldown_until.pop(p, None)
        if self._proxies:
            self._rr_index %= len(self._proxies)
        else:
            self._rr_index = 0

    def __len__(self) -> int:
        return len(self._proxies)

    # ------------------------------------------------------------------ internal

    def _acquire_round_robin(self) -> Optional[str]:
        """v1 原语义：从上次位置继续轮询，冷却中的跳过。"""
        now = self._clock()
        n = len(self._proxies)
        for _ in range(n):
            proxy = self._proxies[self._rr_index % n]
            self._rr_index = (self._rr_index + 1) % n
            until = self._cooldown_until.get(proxy)
            if until is not None and until > now:
                logger.debug(f"Proxy in cooldown, skip: {self._mask(proxy)}")
                continue
            # 冷却已过则清理标记
            self._cooldown_until.pop(proxy, None)
            logger.debug(f"Acquired proxy: {self._mask(proxy)}")
            return proxy

        logger.warning("All proxies in cooldown, acquire returns None")
        return None

    def _acquire_weighted(self) -> Optional[str]:
        """按滑动窗口成功率加权随机选（成功率低的代理被跳过概率高）。"""
        now = self._clock()
        candidates = []
        weights = []
        for p in self._proxies:
            until = self._cooldown_until.get(p)
            if until is not None and until > now:
                continue
            candidates.append(p)
            # 权重下限 0.05：冷启动/低谷代理仍有小概率被探活
            weights.append(max(0.05, self._stats[p].success_rate))

        if not candidates:
            logger.warning("All proxies in cooldown, acquire returns None")
            return None
        return self._rng.choices(candidates, weights=weights, k=1)[0]

    def _escalated_cooldown(self, continuous_fail: int) -> float:
        """动态降权：连续失败达到阶梯阈值后冷却时长按倍率递进。

        base=30min：>=3 次 → 2h，>=6 次 → 12h（取满足的最高档）。
        report_success 会重置 continuous_fail_count，从而退出降权。
        """
        multiplier = 1
        for threshold, mult in COOLDOWN_ESCALATION:
            if continuous_fail >= threshold:
                multiplier = mult
        return self.cooldown_seconds * multiplier

    @staticmethod
    def _mask(proxy_url: str) -> str:
        """日志脱敏：隐藏 user:pass 凭据。"""
        return re.sub(r'//[^@/]+@', '//***@', proxy_url)
