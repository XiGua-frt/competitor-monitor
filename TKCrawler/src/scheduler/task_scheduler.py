"""TaskScheduler：APScheduler 周期调度封装 + 任务队列编排 + JSON 状态持久化。

- schedule_periodic: 注册周期任务（IntervalTrigger），到点触发 run_task 入队。
- run_task: 立即将任务投递到优先级队列（payload 来自 kwargs）。
- pause_task: 暂停周期任务并记录状态。
- load_config: 从 tasks.yaml 批量注册任务定义。
- process_next: 消费队列，执行注册的 handler，失败时按 RetryPolicy 指数退避重新入队。

任务状态持久化：本轮使用 data/scheduler_state.json（原子写）。
后续可无缝迁移到 PostgreSQL 任务状态表（_update_state/_load_state 单点替换即可）。
"""

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from src.common.config import config
from src.common.logger import get_logger
from src.scheduler.retry_policy import RetryPolicy
from src.scheduler.task_queue import Task, TaskQueue

logger = get_logger("scheduler.task_scheduler")

# "2d" / "12h" / "30m" / "45s" -> 秒
INTERVAL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_interval(value: Any) -> int:
    """把任务配置里的 interval 值解析为秒。纯数字按秒处理。"""
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower()
    if text.isdigit():
        return int(text)
    unit, num = text[-1:], text[:-1]
    if unit not in INTERVAL_UNITS or not num:
        raise ValueError(f"无法解析 interval: {value!r}（示例: 2d / 12h / 30m / 45s）")
    return int(float(num) * INTERVAL_UNITS[unit])


class TaskScheduler:
    """周期调度 + 优先级队列编排。

    Args:
        queue: 注入 TaskQueue（默认按 Config 连 Redis，失败自动降级内存）。
        state_file: 任务状态 JSON 持久化路径。
        auto_start: 是否立即启动 BackgroundScheduler（测试时传 False）。
        retry_policy: 未在 tasks.yaml 中显式配置 retry 的任务使用的默认策略。
        config_path: 初始化时加载的 tasks.yaml 路径。
    """

    def __init__(
        self,
        queue: Optional[TaskQueue] = None,
        state_file: str = "data/scheduler_state.json",
        auto_start: bool = True,
        retry_policy: Optional[RetryPolicy] = None,
        config_path: Optional[str] = None,
    ):
        self._queue = queue if queue is not None else TaskQueue()
        self._default_retry_policy = retry_policy or RetryPolicy()
        self._state_file = Path(state_file)
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._task_configs: Dict[str, Dict[str, Any]] = {}
        self._handlers: Dict[str, Callable] = {}
        self._states: Dict[str, Dict[str, Any]] = self._load_state()
        if self._states:
            logger.info(f"已从 {self._state_file} 恢复 {len(self._states)} 条任务状态")

        self._auto_start = auto_start
        self._scheduler = BackgroundScheduler(timezone=config.TIMEZONE)
        if auto_start:
            self._scheduler.start()
            # 重启恢复: 状态文件里 status=scheduled 的周期任务重新注册
            # (interval_seconds 已随状态持久化; paused 任务需手动 resume)
            restored = self.restore_periodic_tasks()
            if restored:
                logger.info(f"已从状态文件恢复 {len(restored)} 个周期任务: {restored}")

        # 队列 mark_complete 回调 → 状态持久化
        self._queue._state_callback = self._on_queue_complete

        if config_path:
            self.load_config(config_path)

    # ---------- 对外接口（Agent D/E/F/G 依赖的契约） ----------

    def schedule_periodic(self, task_name: str, interval: int, priority: int,
                          payload: Optional[Dict[str, Any]] = None) -> str:
        """注册周期任务，到点自动入队执行。返回 task_id（默认即 task_name）。

        payload: 任务每次触发时投递到队列的参数（来自 tasks.yaml 的 payload 段）。
                 NOTE 2026-10-08 联调修复: 此前 periodic 触发不携带 payload,
                 导致 tasks.yaml 配置的 sec_uid 等参数丢失、handler 报缺参。
        """
        seconds = parse_interval(interval)
        task_id = task_name
        self._scheduler.add_job(
            self._on_periodic_fire,
            trigger=IntervalTrigger(seconds=seconds),
            args=[task_name],
            id=task_id,
            name=task_name,
            replace_existing=True,
        )
        self._task_configs[task_name] = {
            "task_id": task_id,
            "interval_seconds": seconds,
            "priority": priority,
            "type": "periodic",
            "payload": dict(payload or {}),
        }
        self._update_state(
            task_id, task_name=task_name, priority=priority, status="scheduled",
            interval_seconds=seconds, type="periodic",
            payload=dict(payload or {}),
        )
        logger.info(f"周期任务已注册: {task_name} 每 {seconds}s 一次, priority={priority}")
        return task_id

    def run_task(self, task_name: str, **kwargs) -> str:
        """立即将任务投递到优先级队列。kwargs 作为 payload。

        kwargs 中的 priority 可覆盖已注册任务的优先级。返回 task_id。
        """
        cfg = self._task_configs.get(task_name, {})
        priority = kwargs.pop("priority", cfg.get("priority", 5))
        task = Task(task_name=task_name, payload=kwargs, priority=priority)
        self._queue.enqueue(task, priority)
        self._update_state(
            task.task_id, task_name=task_name, priority=priority,
            status="queued", attempts=0,
        )
        logger.info(f"任务已入队: {task_name} ({task.task_id}) priority={priority}")
        return task.task_id

    def pause_task(self, task_id: str) -> None:
        """暂停周期任务（调度器未运行时仅记录状态）。"""
        try:
            job = self._scheduler.get_job(task_id)
            if job is not None:
                job.pause()
                logger.info(f"任务已暂停: {task_id}")
        except Exception as exc:
            logger.debug(f"暂停 APScheduler job 失败（调度器可能未运行）: {exc}")
        self._update_state(task_id, status="paused")

    def resume_task(self, task_id: str) -> None:
        """恢复已暂停的周期任务。"""
        try:
            job = self._scheduler.get_job(task_id)
            if job is not None:
                job.resume()
                logger.info(f"任务已恢复: {task_id}")
        except Exception as exc:
            logger.debug(f"恢复 APScheduler job 失败: {exc}")
        self._update_state(task_id, status="scheduled")

    def register_handler(self, task_name: str, func: Callable) -> None:
        """注册任务执行函数，签名 func(**payload)。"""
        self._handlers[task_name] = func

    def restore_periodic_tasks(self) -> list:
        """从状态文件恢复周期任务定义（进程重启后调用）。

        恢复条件: type=="periodic" 且 status=="scheduled" 且尚未注册。
        返回恢复的 task_name 列表。paused 任务不自动恢复（需 resume_task）。
        handler 需调用方自行 register_handler。
        """
        restored: list = []
        for task_id, record in list(self._states.items()):
            if record.get("type") != "periodic":
                continue
            if record.get("status") not in ("scheduled", "paused"):
                continue
            name = record.get("task_name", task_id)
            if name in self._task_configs:
                continue
            existing_ids = {j.id for j in self._scheduler.get_jobs()}
            if task_id in existing_ids:
                continue
            interval = record.get("interval_seconds")
            if not interval:
                logger.warning(f"周期任务 {name} 状态缺少 interval_seconds，跳过恢复")
                continue
            # 先取原始状态: schedule_periodic 会把状态覆盖为 scheduled
            orig_status = record.get("status")
            self.schedule_periodic(
                name, interval, record.get("priority", 5),
                payload=record.get("payload") or {},
            )
            if orig_status == "paused":
                self.pause_task(task_id)
            restored.append(name)
        return restored

    def load_config(self, path) -> Dict[str, str]:
        """解析 tasks.yaml 并注册全部 periodic 任务。返回 {task_name: task_id}。"""
        path = Path(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        task_ids: Dict[str, str] = {}
        for item in data.get("tasks", []):
            name = item["name"]
            retry_cfg = item.get("retry") or {}
            policy = RetryPolicy(
                max_attempts=retry_cfg.get("max_attempts", self._default_retry_policy.max_attempts),
                base_delay=retry_cfg.get("base_delay", self._default_retry_policy.base_delay),
                backoff_factor=self._default_retry_policy.backoff_factor,
                mode="exponential" if retry_cfg.get("backoff", "exponential") == "exponential" else "fixed",
                max_delay=self._default_retry_policy.max_delay,
            )
            if item.get("type", "periodic") == "periodic":
                task_id = self.schedule_periodic(
                    name, item["interval"], item.get("priority", 5),
                    payload=item.get("payload") or {},
                )
                self._task_configs[name]["retry_policy"] = policy
            task_ids[name] = task_id
        logger.info(f"已从 {path} 加载 {len(task_ids)} 个任务定义")
        return task_ids

    def process_next(self) -> bool:
        """消费一个任务：出队 → 执行 handler → 成功标记完成 / 失败按退避策略重试。

        返回是否处理了一个任务（队列为空返回 False）。
        """
        task = self._queue.dequeue()
        if task is None:
            return False
        handler = self._handlers.get(task.task_name)
        if handler is None:
            logger.warning(f"任务 {task.task_name} 未注册 handler，跳过")
            self._queue.mark_complete(task.task_id)
            self._update_state(task.task_id, status="skipped", attempts=task.attempts)
            return True

        self._update_state(task.task_id, status="running", attempts=task.attempts)
        try:
            handler(**(task.payload or {}))
        except Exception as exc:
            policy = self._task_configs.get(task.task_name, {}).get("retry_policy") \
                or self._default_retry_policy
            task.attempts += 1
            if policy.should_retry(task.attempts):
                delay = policy.get_delay(task.attempts)
                self._queue.enqueue(task, task.priority, delay_seconds=delay)
                self._update_state(
                    task.task_id, status="retrying", attempts=task.attempts,
                    next_retry_in=delay,
                )
                logger.warning(
                    f"任务 {task.task_name} 失败（{exc}），第 {task.attempts}/{policy.max_attempts} 次"
                    f"重试将在 {delay}s 后"
                )
            else:
                self._queue.mark_complete(task.task_id)
                self._update_state(task.task_id, status="failed", attempts=task.attempts)
                logger.error(f"任务 {task.task_name} 最终失败（已重试 {task.attempts} 次）: {exc}")
            return False

        self._queue.mark_complete(task.task_id)
        self._update_state(task.task_id, status="completed", attempts=task.attempts)
        logger.info(f"任务执行成功: {task.task_name} ({task.task_id})")
        return True

    def shutdown(self, wait: bool = True) -> None:
        """停止调度器。"""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=wait)

    @property
    def jobs(self):
        """暴露 APScheduler 已注册 job（用于测试与运维检查）。"""
        return self._scheduler.get_jobs()

    # ---------- 内部 ----------

    def _on_periodic_fire(self, task_name: str) -> None:
        """周期触发点：将任务实例投递到队列（携带 tasks.yaml 配置的 payload）。"""
        payload = self._task_configs.get(task_name, {}).get("payload") or {}
        self.run_task(task_name, **payload)

    def _on_queue_complete(self, task_id: str, status: str) -> None:
        self._update_state(task_id, status=status)

    def _update_state(self, task_id: str, **fields: Any) -> None:
        record = self._states.setdefault(
            task_id, {"task_id": task_id, "created_at": datetime.now().isoformat()}
        )
        record.update(fields)
        record["updated_at"] = datetime.now().isoformat()
        self._save_state()

    def _save_state(self) -> None:
        """原子写 JSON，避免写一半损坏。"""
        tmp = self._state_file.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._states, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self._state_file)

    def _load_state(self) -> Dict[str, Dict[str, Any]]:
        if not self._state_file.exists():
            return {}
        try:
            return json.loads(self._state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"状态文件损坏，忽略并重建: {exc}")
            return {}
