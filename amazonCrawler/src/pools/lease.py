"""Simple task lease: acquire_lease(task_type, ttl) context manager.

防止同批次任务并发重复执行。Redis SETNX 分布式锁；Redis 不可用降级本地
文件锁（data/locks/{task_type}.lock）。释放时仅删除自己持有的锁（防误删）。
"""

import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Tuple

try:  # pragma: no cover - 取决于 Agent A 是否已就绪
    from src.common.logger import get_logger
except ImportError:
    from ..anti_detection._common_fallback import get_logger

logger = get_logger(__name__)

_LOCK_KEY_PREFIX = 'amz:lease:'
_LOCK_FILE_DIR = Path('data/locks')


class LeaseConflictException(Exception):
    """同 task_type 的租约已被其他持有者占用。"""


class LeaseManager:
    """任务级租约管理器（Redis SETNX -> 本地文件锁降级）。"""

    def __init__(self, redis_client=None, lock_dir: Optional[Path] = None):
        """Initialize lease manager.

        Args:
            redis_client: 可选 Redis 客户端；None 或不可用实例都降级文件锁
            lock_dir: 文件锁目录，默认 data/locks/
        """
        self._redis = redis_client
        self._lock_dir = Path(lock_dir) if lock_dir else _LOCK_FILE_DIR
        self._redis_ok: Optional[bool] = None  # None = 未探测

    @contextmanager
    def acquire_lease(self, task_type: str, ttl: int = 600,
                      blocking: bool = False) -> Iterator[bool]:
        """获取任务租约的上下文管理器。

        Args:
            task_type: 任务类型标识（如 'pdp_batch'）
            ttl: 租约 TTL 秒数（持有者崩溃后自动过期）
            blocking: True 时冲突则自旋等待直至获得（受 ttl 上限保护）；
                False（默认）冲突立即抛 LeaseConflictException

        Yields:
            True 表示成功持有租约（with 块内独占）

        Raises:
            LeaseConflictException: 非阻塞模式下租约已被占用
        """
        holder_id = f"{os.getpid()}:{uuid.uuid4().hex[:12]}"
        token = json.dumps({'holder': holder_id, 'ts': time.time()})
        key = f"{_LOCK_KEY_PREFIX}{task_type}"
        deadline = time.time() + max(1, int(ttl))

        acquired, backend = self._try_acquire(key, token, ttl)
        if not acquired and blocking:
            while time.time() < deadline:
                time.sleep(0.2)
                acquired, backend = self._try_acquire(key, token, ttl)
                if acquired:
                    break

        if not acquired:
            raise LeaseConflictException(
                f"Lease conflict on '{task_type}' (held by another owner)"
            )

        logger.info(f"Lease acquired [{backend}]: {task_type} holder={holder_id}")
        try:
            yield True
        finally:
            self._release(key, token, backend)
            logger.info(f"Lease released [{backend}]: {task_type}")

    # ------------------------------------------------------------------ internal

    def _try_acquire(self, key: str, token: str, ttl: int) -> Tuple[bool, str]:
        """尝试一次加锁，返回 (成功, 后端)。Redis 失败自动降级文件锁。"""
        if self._redis_ok is not False and self._redis is not None:
            try:
                # nx=True 且 key 不存在时才写入 → 原子 SETNX 语义
                ok = self._redis.set(key, token, ex=int(ttl), nx=True)
                self._redis_ok = True
                if ok:
                    return True, 'redis'
                return False, 'redis'
            except Exception as e:
                logger.warning(f"Redis lease unavailable, fallback to file lock: {e}")
                self._redis_ok = False

        return self._try_acquire_file(key, token, ttl), 'file'

    def _lock_file(self, key: str) -> Path:
        """Redis key -> 本地锁文件路径。"""
        safe = key.replace(_LOCK_KEY_PREFIX, '').replace(':', '_')
        return self._lock_dir / f"{safe}.lock"

    def _try_acquire_file(self, key: str, token: str, ttl: int) -> bool:
        """文件锁降级路径：O_CREAT|O_EXCL 原子创建 + TTL 过期清理。"""
        path = self._lock_file(key)
        self._lock_dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            # 已存在：检查是否过期（持有者崩溃残留）
            try:
                age = time.time() - path.stat().st_mtime
                if age > ttl:
                    logger.warning(
                        f"Stale file lease expired (age {age:.0f}s > {ttl}s), "
                        f"reclaiming: {path}"
                    )
                    path.unlink(missing_ok=True)
                    return self._try_acquire_file(key, token, ttl)
            except OSError:
                pass
            return False
        else:
            with os.fdopen(fd, 'w') as f:
                f.write(token)
            return True

    def _release(self, key: str, token: str, backend: str) -> None:
        """释放租约；只删除自己持有的锁（Redis 比对 token，文件锁比对内容）。"""
        try:
            if backend == 'redis' and self._redis is not None:
                current = self._redis.get(key)
                if current == token:
                    self._redis.delete(key)
                elif current is not None:
                    logger.warning("Lease token mismatch on release, skip delete")
                return

            path = self._lock_file(key)
            if path.exists():
                try:
                    if path.read_text(encoding='utf-8') == token:
                        path.unlink()
                    else:
                        logger.warning("File lease token mismatch, skip unlink")
                except OSError as e:
                    logger.warning(f"File lease release error: {e}")
        except Exception as e:
            logger.warning(f"Lease release error: {e}")
