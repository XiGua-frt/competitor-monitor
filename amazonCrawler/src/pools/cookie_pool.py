"""Amazon profile 会话池（CookiePool）——region profile 维度，不是账号。

Amazon 前台无账号体系，会话维度 = region profile（marketplace + 邮编 +
币种 + 语言）。profile_key 为 sha256 前 24 位（参考 TK repository.py）。
单 profile 时语义退化为 SessionManager 等价物。
"""

import hashlib
import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional

try:  # pragma: no cover - 取决于 Agent A 是否已就绪
    from src.common.logger import get_logger
except ImportError:
    from ..anti_detection._common_fallback import get_logger

logger = get_logger(__name__)

DEFAULT_STORAGE_PATH = 'data/amz_cookie_pool.json'
PROFILE_COOLDOWN_SECONDS = 30 * 60  # 单 profile 冷却（如被 503 标记）


def compute_profile_key(marketplace: str, delivery_postcode: str,
                        currency: str, language: str) -> str:
    """profile 稳定标识：四元组拼接后 sha256 前 24 位（与字段顺序无关的规范化拼接）。"""
    normalized = '|'.join([
        (marketplace or '').strip().lower(),
        (delivery_postcode or '').strip(),
        (currency or '').strip().upper(),
        (language or '').strip(),
    ])
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:24]


@dataclass
class SessionProfile:
    """region profile：Amazon 会话身份的完整描述。"""

    marketplace: str
    delivery_postcode: str = ''
    currency: str = 'USD'
    language: str = 'en-US'
    profile_key: str = field(default='', compare=False)

    def __post_init__(self):
        if not self.profile_key:
            self.profile_key = compute_profile_key(
                self.marketplace, self.delivery_postcode,
                self.currency, self.language)


@dataclass
class CookieLease:
    """acquire 返回的租约条目。"""

    profile: SessionProfile
    cookies: Dict[str, str]
    last_used: float = 0.0


class CookiePool:
    """region profile 会话池：round-robin 租约 + 单 profile 冷却 + JSON 持久化。"""

    def __init__(self, profiles: List[SessionProfile],
                 storage_path=DEFAULT_STORAGE_PATH,
                 cooldown_seconds: float = PROFILE_COOLDOWN_SECONDS,
                 clock=time.time):
        """Initialize cookie pool.

        Args:
            profiles: SessionProfile 列表（单成员 = 退化为 SessionManager 等价物）
            storage_path: JSON 持久化路径（cookies 按 profile_key 存储）
            cooldown_seconds: profile 失败冷却时长（默认 30 分钟）
            clock: 时间函数（测试注入假时钟）
        """
        if not profiles:
            raise ValueError('CookiePool requires at least one SessionProfile')
        self.profiles: List[SessionProfile] = list(profiles)
        self.storage_path = Path(storage_path)
        self.cooldown_seconds = float(cooldown_seconds)
        self._clock = clock
        self._rr_index = 0
        # profile_key -> 冷却到期时刻
        self._cooldown_until: Dict[str, float] = {}
        # profile_key -> cookies dict
        self._cookies: Dict[str, Dict[str, str]] = {}
        # profile_key -> last_used 时间戳
        self._last_used: Dict[str, float] = {}
        self.load()

    # ------------------------------------------------------------------ 租约

    def acquire(self, profile_key: Optional[str] = None) -> Optional[CookieLease]:
        """取下一个健康 profile 租约；指定 key 时精确取；全冷却返回 None。

        round-robin 语义与 IPPool 一致：从上次位置继续，冷却中的跳过。
        """
        if profile_key is not None:
            profile = self._find(profile_key)
            if profile is None:
                logger.warning(f"Unknown profile_key: {profile_key[:8]}…")
                return None
            if self._in_cooldown(profile.profile_key):
                return None
            return self._make_lease(profile)

        now = self._clock()
        n = len(self.profiles)
        for _ in range(n):
            profile = self.profiles[self._rr_index % n]
            self._rr_index = (self._rr_index + 1) % n
            until = self._cooldown_until.get(profile.profile_key)
            if until is not None and until > now:
                continue
            self._cooldown_until.pop(profile.profile_key, None)
            return self._make_lease(profile)

        logger.warning('All profiles in cooldown, acquire returns None')
        return None

    @contextmanager
    def lease(self, profile_key: Optional[str] = None) -> Iterator[Optional[CookieLease]]:
        """租约上下文：正常退出 report_success，异常退出 report_failure。"""
        entry = self.acquire(profile_key)
        try:
            yield entry
        except Exception:
            if entry is not None:
                self.report_failure(entry.profile.profile_key)
            raise
        else:
            if entry is not None:
                self.report_success(entry.profile.profile_key)

    def report_failure(self, profile_key: str) -> None:
        """标记 profile 失败，进入冷却（滑动重置）。"""
        if self._find(profile_key) is None:
            logger.warning(f"report_failure for unknown profile: {profile_key[:8]}…")
            return
        self._cooldown_until[profile_key] = self._clock() + self.cooldown_seconds
        logger.warning(
            f"Profile in cooldown {self.cooldown_seconds:.0f}s: "
            f"{profile_key[:8]}…")

    def report_success(self, profile_key: str) -> None:
        """成功：清除冷却标记并更新 last_used。"""
        if self._find(profile_key) is None:
            return
        self._cooldown_until.pop(profile_key, None)
        self._last_used[profile_key] = self._clock()

    def healthy_count(self) -> int:
        """当前不在冷却中的 profile 数量。"""
        now = self._clock()
        return sum(
            1 for p in self.profiles
            if self._cooldown_until.get(p.profile_key, float('-inf')) <= now)

    def set_cookies(self, profile_key: str, cookies: Dict[str, str]) -> None:
        """写入/更新 profile 的 cookies（会话刷新后调用）。"""
        if self._find(profile_key) is None:
            logger.warning(f"set_cookies for unknown profile: {profile_key[:8]}…")
            return
        self._cookies[profile_key] = dict(cookies or {})

    # ------------------------------------------------------------------ 持久化

    def save(self) -> None:
        """JSON 持久化：profiles + cookies + 冷却剩余时间。"""
        now = self._clock()
        payload = {
            'version': 1,
            'saved_at': now,
            'profiles': [
                {
                    'profile_key': p.profile_key,
                    'marketplace': p.marketplace,
                    'delivery_postcode': p.delivery_postcode,
                    'currency': p.currency,
                    'language': p.language,
                }
                for p in self.profiles
            ],
            'cookies': self._cookies,
            'cooldown_remaining': {
                k: max(0.0, until - now)
                for k, until in self._cooldown_until.items() if until > now
            },
        }
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.storage_path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding='utf-8')
        tmp.replace(self.storage_path)

    def load(self) -> bool:
        """从 JSON 恢复 cookies 与冷却状态；文件缺失/损坏静默容错。

        Returns:
            True 表示成功恢复；False 表示冷启动（无文件/损坏/空）。
        """
        try:
            raw = self.storage_path.read_text(encoding='utf-8')
            data = json.loads(raw)
        except FileNotFoundError:
            return False
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            # 损坏文件不阻塞启动：记 WARNING 后按冷启动处理，下次 save 覆盖
            logger.warning(
                f"Cookie pool storage corrupted, starting cold: {exc}")
            return False
        if not isinstance(data, dict):
            logger.warning('Cookie pool storage unexpected format, ignored')
            return False

        now = self._clock()
        cookies = data.get('cookies') or {}
        if isinstance(cookies, dict):
            self._cookies = {
                k: dict(v) for k, v in cookies.items()
                if isinstance(v, dict) and self._find(k) is not None
            }
        remaining = data.get('cooldown_remaining') or {}
        if isinstance(remaining, dict):
            for key, secs in remaining.items():
                try:
                    self._cooldown_until[key] = now + float(secs)
                except (TypeError, ValueError):
                    continue
        logger.info(
            f"Cookie pool loaded: {len(self._cookies)} cookie sets "
            f"for {len(self.profiles)} profiles")
        return True

    def __len__(self) -> int:
        return len(self.profiles)

    # ------------------------------------------------------------------ internal

    def _find(self, profile_key: str) -> Optional[SessionProfile]:
        for p in self.profiles:
            if p.profile_key == profile_key:
                return p
        return None

    def _in_cooldown(self, profile_key: str) -> bool:
        until = self._cooldown_until.get(profile_key)
        return until is not None and until > self._clock()

    def _make_lease(self, profile: SessionProfile) -> CookieLease:
        now = self._clock()
        self._last_used[profile.profile_key] = now
        return CookieLease(
            profile=profile,
            cookies=dict(self._cookies.get(profile.profile_key, {})),
            last_used=now,
        )
