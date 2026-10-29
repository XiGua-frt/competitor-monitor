"""TaskScheduler — APScheduler 周期调度 + 内存优先队列 + 指数退避重试。

精简自 TKCrawler/src/scheduler/task_scheduler.py：
- load_config(path): 解析 tasks.yaml（interval/priority/retry）；
- register_handler(name, fn) + start()：周期任务到点投递内存优先队列；
- 后台 worker 线程消费队列，失败按 retry.exponential(base_delay) 重新入队
  （最多 max_attempts 次）；
- 任务状态 JSON 原子持久化（state_file），重启可恢复周期任务注册。
"""

import json
import queue
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from src.common.config import config
from src.common.logger import get_logger

logger = get_logger("scheduler.task_scheduler")

# "2d" / "12h" / "30m" / "45s" -> 秒
INTERVAL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_interval(value: Any) -> int:
    """把 interval 配置解析为秒：'2d'/'12h'/'30m'/'45s'/纯数字（秒）。"""
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower()
    if text.isdigit():
        return int(text)
    unit, num = text[-1:], text[:-1]
    if unit not in INTERVAL_UNITS or not num:
        raise ValueError(
            f"无法解析 interval: {value!r}（示例: 2d / 12h / 30m / 45s）")
    return int(float(num) * INTERVAL_UNITS[unit])


@dataclass(order=True)
class Task:
    """优先队列条目：priority 数字越小越先执行（同优先级 FIFO）。"""

    priority: int
    seq: int = field(compare=True)
    task_id: str = field(compare=False)
    task_name: str = field(compare=False)
    payload: dict = field(compare=False, default_factory=dict)
    attempts: int = field(compare=False, default=0)
    not_before: float = field(compare=False, default=0.0)  # 退避重入队


@dataclass
class RetryPolicy:
    """重试策略：exponential → delay = base_delay * 2^(attempts-1)。"""

    max_attempts: int = 3
    base_delay: float = 60.0
    mode: str = "exponential"

    def get_delay(self, attempts: int) -> float:
        if self.mode == "exponential":
            return float(self.base_delay) * (2 ** max(0, attempts - 1))
        return float(self.base_delay)

    def should_retry(self, attempts: int) -> bool:
        """attempts 为已失败次数；不超过 max_attempts 就再试。"""
        return attempts < self.max_attempts


class TaskScheduler:
    """周期调度 + 内存优先队列 + worker 线程。

    Args:
        queue: 注入 queue.PriorityQueue（默认自建）。
        state_file: 任务状态 JSON 持久化路径（原子写）。
        auto_start: True 时构造即启动 APScheduler 与 worker（测试传 False）。
        config_path: 初始加载的 tasks.yaml 路径（None 不加载）。
    """

    def __init__(self, queue: Optional["queue.PriorityQueue"] = None,
                 state_file: str = "data/scheduler_state.json",
                 auto_start: bool = False,
                 config_path: Optional[str] = None):
        # queue 参数遮蔽了 queue 模块名，先解析模块引用
        _queue_mod = __import__("queue")
        self._queue = queue if queue is not None else _queue_mod.PriorityQueue()
        self._state_file = Path(state_file)
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
        except OSError:  # pragma: no cover - 只读环境
            pass
        self._task_configs: Dict[str, Dict[str, Any]] = {}
        self._handlers: Dict[str, Callable] = {}
        self._states: Dict[str, Dict[str, Any]] = self._load_state()

        self._scheduler = BackgroundScheduler(timezone=config.TIMEZONE)
        self._worker: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._seq = 0
        self._seq_lock = threading.Lock()

        if config_path:
            self.load_config(config_path)
        if auto_start:
            self.start()

    # ------------------------------------------------------------------
    # 配置加载 / handler 注册
    # ------------------------------------------------------------------
    def load_config(self, path) -> Dict[str, str]:
        """解析 tasks.yaml 并注册全部 periodic 任务，返回 {task_name: task_id}。"""
        path = Path(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        task_ids: Dict[str, str] = {}
        for item in data.get("tasks", []):
            name = item["name"]
            retry_cfg = item.get("retry") or {}
            policy = RetryPolicy(
                max_attempts=int(retry_cfg.get("max_attempts", 3)),
                base_delay=float(retry_cfg.get("base_delay", 60)),
                mode="exponential"
                if retry_cfg.get("backoff", "exponential") == "exponential"
                else "fixed",
            )
            task_id = self.schedule_periodic(
                name, parse_interval(item["interval"]),
                int(item.get("priority", 5)),
                payload=item.get("payload") or {},
            )
            self._task_configs[name]["retry_policy"] = policy
            task_ids[name] = task_id
        logger.info("已从 {} 加载 {} 个任务定义", path, len(task_ids))
        return task_ids

    def schedule_periodic(self, task_name: str, interval: int, priority: int,
                          payload: Optional[Dict[str, Any]] = None) -> str:
        """注册周期任务（IntervalTrigger），到点自动投递队列。返回 task_id。"""
        task_id = task_name
        self._scheduler.add_job(
            self._on_periodic_fire,
            trigger=IntervalTrigger(seconds=interval),
            args=[task_name],
            id=task_id,
            name=task_name,
            replace_existing=True,
        )
        self._task_configs[task_name] = {
            "task_id": task_id,
            "interval_seconds": int(interval),
            "priority": priority,
            "type": "periodic",
            "payload": dict(payload or {}),
        }
        self._update_state(
            task_id, task_name=task_name, priority=priority,
            status="scheduled", interval_seconds=int(interval),
            type="periodic", payload=dict(payload or {}),
        )
        return task_id

    def register_handler(self, task_name: str, func: Callable) -> None:
        """注册任务执行函数，签名 func(**payload)。"""
        self._handlers[task_name] = func

    # ------------------------------------------------------------------
    # 启停
    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动 APScheduler + 后台 worker 线程（幂等）。"""
        if not self._scheduler.running:
            self._scheduler.start()
        if self._worker is None or not self._worker.is_alive():
            self._stop_event.clear()
            self._worker = threading.Thread(
                target=self._worker_loop, name="scheduler-worker", daemon=True)
            self._worker.start()

    def stop(self, wait: bool = True) -> None:
        """停 worker + APScheduler。"""
        self._stop_event.set()
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=5 if wait else 0)
            self._worker = None
        if self._scheduler.running:
            try:
                self._scheduler.shutdown(wait=wait)
            except Exception:  # pragma: no cover
                pass

    def run_forever(self) -> None:
        """前台阻塞运行（CLI 用）：start 后阻塞直到 Ctrl-C。"""
        import time
        self.start()
        try:
            while True:
                time.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            self.stop()

    def status(self) -> dict:
        """调度器状态快照（运维/测试检查）。"""
        return {
            "scheduler_running": self._scheduler.running,
            "worker_alive": bool(self._worker and self._worker.is_alive()),
            "registered_tasks": sorted(self._task_configs),
            "registered_handlers": sorted(self._handlers),
            "queue_size": self._queue.qsize(),
            "states": {k: v.get("status") for k, v in self._states.items()},
        }

    @property
    def jobs(self):
        """暴露 APScheduler 已注册 job（测试用）。"""
        return self._scheduler.get_jobs()

    # ------------------------------------------------------------------
    # 入队 / 消费
    # ------------------------------------------------------------------
    def submit(self, task_name: str, **payload) -> str:
        """立即把一个任务实例投递到优先级队列。返回 task_id。"""
        cfg = self._task_configs.get(task_name, {})
        priority = payload.pop("priority", cfg.get("priority", 5))
        with self._seq_lock:
            self._seq += 1
            seq = self._seq
        task = Task(priority=priority, seq=seq,
                    task_id=uuid.uuid4().hex[:12], task_name=task_name,
                    payload=payload)
        self._queue.put(task)
        self._update_state(task.task_id, task_name=task_name,
                           priority=priority, status="queued", attempts=0)
        return task.task_id

    def process_next(self, timeout: float = None) -> bool:
        """消费一个任务：执行 handler → 成功完成 / 失败退避重入队 / 重试耗尽失败。

        Returns:
            是否处理了一个任务（timeout 内无任务返回 False）。
        """
        try:
            task = self._queue.get(timeout=timeout) if timeout is not None \
                else self._queue.get_nowait()
        except (__import__("queue").Empty):
            return False

        # 退避重入队的任务还没到期 → 放回去并稍等
        if task.not_before > datetime.now().timestamp():
            self._queue.put(task)
            return False

        handler = self._handlers.get(task.task_name)
        if handler is None:
            logger.warning("任务 {} 未注册 handler，跳过", task.task_name)
            self._update_state(task.task_id, status="skipped")
            return True

        self._update_state(task.task_id, status="running",
                           attempts=task.attempts)
        try:
            handler(**(task.payload or {}))
        except Exception as exc:
            policy = self._task_configs.get(task.task_name, {}).get(
                "retry_policy") or RetryPolicy()
            task.attempts += 1
            if policy.should_retry(task.attempts):
                delay = policy.get_delay(task.attempts)
                task.not_before = datetime.now().timestamp() + delay
                with self._seq_lock:
                    self._seq += 1
                    task.seq = self._seq
                self._queue.put(task)
                self._update_state(
                    task.task_id, status="retrying", attempts=task.attempts,
                    next_retry_in=delay)
                logger.warning("任务 {} 失败（{}），第 {}/{} 次重试将在 {}s 后",
                               task.task_name, exc, task.attempts,
                               policy.max_attempts, delay)
            else:
                self._update_state(task.task_id, status="failed",
                                   attempts=task.attempts)
                logger.error("任务 {} 最终失败（已试 {} 次）: {}",
                             task.task_name, task.attempts, exc)
            return True

        self._update_state(task.task_id, status="completed",
                           attempts=task.attempts)
        return True

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _worker_loop(self) -> None:
        """后台消费循环：有任务立即处理，空队列轮询等待停止信号。"""
        import time
        while not self._stop_event.is_set():
            processed = False
            try:
                task = self._queue.get(timeout=0.5)
            except __import__("queue").Empty:
                continue
            try:
                # 借 process_next 的核心逻辑但跳过二次 get：直接复用
                processed = self._execute(task)
            except Exception:  # pragma: no cover
                logger.exception("worker 处理任务异常")
            if not processed:
                time.sleep(0.1)

    def _execute(self, task: Task) -> bool:
        """执行单个已出队任务（process_next 的共享核心）。"""
        handler = self._handlers.get(task.task_name)
        if handler is None:
            logger.warning("任务 {} 未注册 handler，跳过", task.task_name)
            self._update_state(task.task_id, status="skipped")
            return True
        self._update_state(task.task_id, status="running",
                           attempts=task.attempts)
        try:
            handler(**(task.payload or {}))
        except Exception as exc:
            policy = self._task_configs.get(task.task_name, {}).get(
                "retry_policy") or RetryPolicy()
            task.attempts += 1
            if policy.should_retry(task.attempts):
                delay = policy.get_delay(task.attempts)
                task.not_before = datetime.now().timestamp() + delay
                with self._seq_lock:
                    self._seq += 1
                    task.seq = self._seq
                self._queue.put(task)
                self._update_state(task.task_id, status="retrying",
                                   attempts=task.attempts, next_retry_in=delay)
                logger.warning("任务 {} 失败（{}），第 {}/{} 次重试将在 {}s 后",
                               task.task_name, exc, task.attempts,
                               policy.max_attempts, delay)
            else:
                self._update_state(task.task_id, status="failed",
                                   attempts=task.attempts)
                logger.error("任务 {} 最终失败（已试 {} 次）: {}",
                             task.task_name, task.attempts, exc)
            return True
        self._update_state(task.task_id, status="completed",
                           attempts=task.attempts)
        return True

    def _on_periodic_fire(self, task_name: str) -> None:
        """周期触发点：携带 tasks.yaml payload 投递队列。"""
        payload = self._task_configs.get(task_name, {}).get("payload") or {}
        self.submit(task_name, **payload)

    def restore_periodic_tasks(self) -> List[str]:
        """进程重启后从状态文件恢复 scheduled 状态的周期任务（handler 需另注册）。"""
        restored: List[str] = []
        for task_id, record in list(self._states.items()):
            if record.get("type") != "periodic":
                continue
            if record.get("status") != "scheduled":
                continue
            name = record.get("task_name", task_id)
            if name in self._task_configs:
                continue
            interval = record.get("interval_seconds")
            if not interval:
                continue
            self.schedule_periodic(
                name, interval, record.get("priority", 5),
                payload=record.get("payload") or {})
            restored.append(name)
        return restored

    # ------------------------------------------------------------------
    # 状态持久化（原子写 JSON）
    # ------------------------------------------------------------------
    def _update_state(self, task_id: str, **fields: Any) -> None:
        record = self._states.setdefault(
            task_id, {"task_id": task_id,
                      "created_at": datetime.now().isoformat()})
        record.update(fields)
        record["updated_at"] = datetime.now().isoformat()
        self._save_state()

    def _save_state(self) -> None:
        try:
            tmp = self._state_file.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._states, ensure_ascii=False, indent=2),
                encoding="utf-8")
            tmp.replace(self._state_file)
        except OSError as exc:  # pragma: no cover - 只读环境
            logger.debug("状态文件写入失败: {}", exc)

    def _load_state(self) -> Dict[str, Dict[str, Any]]:
        if not self._state_file.exists():
            return {}
        try:
            return json.loads(self._state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("状态文件损坏，忽略并重建: {}", exc)
            return {}
