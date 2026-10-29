"""Redis 优先级任务队列。

- 使用 Redis sorted set 实现：score = priority * 1e13 + created_at_ms。
  优先级数字越小优先级越高（P1 先于 P2），同优先级按入队时间 FIFO。
- 支持延迟入队（delay_seconds），用于指数退避重试。
- Redis 不可用时优雅降级为内存队列（heapq），并输出 WARNING 日志。
"""

import heapq
import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

import redis

from src.common.config import config
from src.common.logger import get_logger

logger = get_logger("scheduler.task_queue")

QUEUE_KEY = "scheduler:task_queue"          # sorted set: member=task json, score=排序分
PROCESSING_KEY = "scheduler:task_processing"  # 取出但未完成的任务
TASK_META_KEY = "scheduler:task_meta"       # hash: task_id -> task json

# score 缩放：priority 占高位，created_at_ms 占低位（当前毫秒时间戳 ~1.8e12 < 1e13，
# float64 精确整数上限 2^53≈9e15，故 priority*1e13 + ms 无精度损失）。
_PRIORITY_SCALE = 10 ** 13
_DEQUEUE_SCAN_LIMIT = 100  # dequeue 单次扫描的最大条目数


def _score(priority: int, available_at_ms: int) -> float:
    return priority * _PRIORITY_SCALE + available_at_ms


@dataclass
class Task:
    """队列中的最小任务单元。payload 必须可 JSON 序列化。"""

    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    task_name: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    priority: int = 5
    created_at: float = field(default_factory=time.time)
    attempts: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "Task":
        data = json.loads(raw)
        return cls(**data)


class TaskQueue:
    """Redis 优先级队列，Redis 不可用时降级为内存队列。

    Args:
        redis_client: 注入现成客户端（测试用 fakeredis）。
        use_redis: False 时直接进入内存降级模式。
        state_callback: mark_complete 时回调 (task_id, status)，用于状态持久化。
    """

    def __init__(
        self,
        redis_client: Optional["redis.Redis"] = None,
        use_redis: bool = True,
        state_callback: Optional[Callable[[str, str], None]] = None,
    ):
        self._lock = threading.Lock()
        self._state_callback = state_callback
        self.degraded = False

        # 内存后备结构
        self._memory_heap: List[tuple] = []       # (score, task_json)
        self._memory_processing: Dict[str, tuple] = {}  # task_id -> (score, task_json)

        self._redis = None
        if redis_client is not None:
            try:
                redis_client.ping()
                self._redis = redis_client
            except Exception as exc:
                logger.warning(f"注入的 Redis 客户端不可用（{exc}），降级为内存队列")
                self.degraded = True
        elif use_redis:
            try:
                client = redis.Redis(
                    host=config.REDIS_HOST,
                    port=config.REDIS_PORT,
                    password=config.REDIS_PASSWORD,
                    db=config.REDIS_DB,
                    decode_responses=True,
                    socket_connect_timeout=2,
                )
                client.ping()
                self._redis = client
                logger.info(f"任务队列已连接 Redis {config.REDIS_HOST}:{config.REDIS_PORT}")
            except Exception as exc:
                logger.warning(
                    f"Redis 不可用（{exc}），任务队列降级为内存后备模式，"
                    "重启后队列数据将丢失"
                )
                self.degraded = True
        else:
            logger.warning("任务队列运行在内存降级模式（use_redis=False），重启后队列数据将丢失")
            self.degraded = True

    # ---------- 公开接口 ----------

    def enqueue(self, task: Task, priority: int, delay_seconds: int = 0) -> str:
        """入队。priority 越小越先出队；delay_seconds>0 时延迟可见（用于重试退避）。

        FIFO 时间基准：无延迟时用 task.created_at（保证同级按创建顺序出队）；
        有延迟时用 now + delay_seconds 作为可见时间。
        """
        task.priority = priority
        if delay_seconds > 0:
            available_at_ms = int((time.time() + delay_seconds) * 1000)
        else:
            available_at_ms = int(task.created_at * 1000)
        score = _score(priority, available_at_ms)
        raw = task.to_json()
        if self._redis is not None:
            try:
                pipe = self._redis.pipeline()
                pipe.zadd(QUEUE_KEY, {raw: score})
                pipe.hset(TASK_META_KEY, task.task_id, raw)
                pipe.execute()
                logger.debug(f"任务入队 {task.task_id} ({task.task_name}) priority={priority} delay={delay_seconds}s")
                return task.task_id
            except Exception as exc:
                self._on_redis_failure(exc)
        with self._lock:
            heapq.heappush(self._memory_heap, (score, raw))
        logger.debug(f"任务入队(内存) {task.task_id} ({task.task_name}) priority={priority} delay={delay_seconds}s")
        return task.task_id

    def dequeue(self) -> Optional[Task]:
        """取出当前可执行的最高优先级任务；无可用任务返回 None。

        score 高位是优先级，无法用单一 zrangebyscore 上界同时表达
        "按优先级排序" 与 "按到期时间过滤"，因此按 score 升序扫描前 N 项，
        取第一个到期的任务（= 可用任务中优先级最高、同级最早的）。
        """
        now_ms = int(time.time() * 1000)
        if self._redis is not None:
            try:
                candidates = self._redis.zrange(QUEUE_KEY, 0, _DEQUEUE_SCAN_LIMIT - 1)
                for raw in candidates:
                    score = self._redis.zscore(QUEUE_KEY, raw)
                    if score is None:
                        continue
                    if (score % _PRIORITY_SCALE) <= now_ms:
                        if self._redis.zrem(QUEUE_KEY, raw):
                            task = Task.from_json(raw)
                            self._redis.zadd(PROCESSING_KEY, {raw: score})
                            return task
                    # 未到期也继续扫：低优先级任务可能已到期
                return None
            except Exception as exc:
                self._on_redis_failure(exc)
        with self._lock:
            # 弹出直到找到第一个时间可用（score 低 13 位时间戳 <= now）的项；
            # 由于 score 高位是优先级，堆序保证弹出的第一个可用项即最高优先级、同级最早。
            skipped = []
            best = None
            while self._memory_heap:
                item = heapq.heappop(self._memory_heap)
                if (item[0] % _PRIORITY_SCALE) <= now_ms:
                    best = item
                    break
                skipped.append(item)
            for item in skipped:
                heapq.heappush(self._memory_heap, item)
            if best is None:
                return None
            task = Task.from_json(best[1])
            self._memory_processing[task.task_id] = best
            return task

    def mark_complete(self, task_id: str) -> None:
        """任务处理完成：移出 processing 集合并清理元数据。"""
        if self._redis is not None:
            try:
                raw = self._redis.hget(TASK_META_KEY, task_id)
                if raw:
                    self._redis.zrem(PROCESSING_KEY, raw)
                self._redis.hdel(TASK_META_KEY, task_id)
                self._notify_complete(task_id)
                return
            except Exception as exc:
                self._on_redis_failure(exc)
        with self._lock:
            self._memory_processing.pop(task_id, None)
        self._notify_complete(task_id)

    def qsize(self) -> int:
        """当前待处理（含延迟中）任务数。"""
        if self._redis is not None:
            try:
                return int(self._redis.zcard(QUEUE_KEY))
            except Exception as exc:
                self._on_redis_failure(exc)
        with self._lock:
            return len(self._memory_heap)

    # ---------- 内部 ----------

    def _notify_complete(self, task_id: str) -> None:
        if self._state_callback:
            try:
                self._state_callback(task_id, "completed")
            except Exception as exc:
                logger.warning(f"状态回调失败: {exc}")

    def _on_redis_failure(self, exc: Exception) -> None:
        """运行中 Redis 故障：切换到内存降级模式。"""
        if not self.degraded:
            logger.warning(f"Redis 操作失败（{exc}），任务队列降级为内存后备模式")
        self._redis = None
        self.degraded = True
