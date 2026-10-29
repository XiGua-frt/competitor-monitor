"""WatchlistService — 竞品监控清单的增删查（读写 src/scheduler/config/watchlist.yaml）。

CLI 与 MCP 的唯一入口（薄逻辑，不掺抓取）。
"""

import re
import threading
from pathlib import Path
from typing import List, Optional

import yaml

try:
    from src.common.logger import get_logger
    logger = get_logger(__name__)
except ImportError:  # pragma: no cover
    import logging
    logger = logging.getLogger(__name__)

# ASIN 格式：B0 开头 + 8 位大写字母/数字
ASIN_PATTERN = re.compile(r"^B0[A-Z0-9]{8}$")

# 默认清单文件（调度与 MCP 共用；测试注入临时路径覆盖）
DEFAULT_WATCHLIST_PATH = Path(__file__).resolve().parent.parent / \
    "scheduler" / "config" / "watchlist.yaml"


class WatchlistValidationError(ValueError):
    """ASIN 等字段校验失败。"""


class WatchlistService:
    """watchlist.yaml 的线程安全读写层。"""

    def __init__(self, path: Optional[Path] = None):
        self._path = Path(path) if path else DEFAULT_WATCHLIST_PATH
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 内部：加载 / 落盘（调用方持锁）
    # ------------------------------------------------------------------
    def _load(self) -> list:
        if not self._path.exists():
            return []
        try:
            data = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError) as exc:
            logger.error("watchlist.yaml 解析失败，按空清单处理: {}", exc)
            return []
        items = data.get("watchlist") or []
        # 过滤脏条目（缺 asin 的行）
        return [it for it in items if isinstance(it, dict) and it.get("asin")]

    def _save(self, items: list) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"watchlist": items}
        self._path.write_text(
            yaml.safe_dump(payload, allow_unicode=True,
                           default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def list_items(self) -> List[dict]:
        """全部清单条目 [{asin, note, variant_asin}]。"""
        with self._lock:
            return [dict(it) for it in self._load()]

    def add_item(self, asin: str, note: str = "",
                 variant_asin: Optional[str] = None) -> dict:
        """新增（或更新 note/variant_asin）一条清单记录，返回该条目。

        Raises:
            WatchlistValidationError: ASIN 不匹配 ^B0[A-Z0-9]{8}$。
        """
        asin = (asin or "").strip().upper()
        if not ASIN_PATTERN.match(asin):
            raise WatchlistValidationError(f"非法 ASIN: {asin!r}")
        if variant_asin is not None:
            variant_asin = variant_asin.strip().upper() or None
        with self._lock:
            items = self._load()
            for it in items:
                if it.get("asin") == asin:
                    # 已存在 → 更新可选字段，保持幂等
                    it["note"] = note or it.get("note", "")
                    it["variant_asin"] = variant_asin or it.get("variant_asin")
                    self._save(items)
                    return dict(it)
            entry = {"asin": asin, "note": note or "",
                     "variant_asin": variant_asin}
            items.append(entry)
            self._save(items)
        logger.info("watchlist 新增: {} (note={})", asin, note)
        return dict(entry)

    def remove_item(self, asin: str) -> bool:
        """删除一条记录；不存在返回 False。"""
        asin = (asin or "").strip().upper()
        with self._lock:
            items = self._load()
            kept = [it for it in items if it.get("asin") != asin]
            if len(kept) == len(items):
                return False
            self._save(kept)
        logger.info("watchlist 移除: {}", asin)
        return True
