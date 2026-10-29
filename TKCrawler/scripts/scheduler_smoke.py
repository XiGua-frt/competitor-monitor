#!/usr/bin/env python3
"""scheduler 真实触发链路冒烟（Agent Q, 2026-10-07）。

目的：在 Docker 里 PostgreSQL/Redis 已运行的前提下，验证 TaskScheduler 的
"周期触发 -> 入队 -> process_next 消费 -> 状态持久化" 全链路真实可用。

纪律：
- 不加载 tasks.yaml，只注册一个 smoke_tick 任务；
- handler 只打日志 + 追加一行到 data/scheduler_smoke.log，绝不做任何网络请求；
- 阻塞跑 ~35 秒（10s 间隔至少 3 次真实触发），优雅退出（Ctrl+C 同样走清理路径）；
- 不改 src/scheduler/** 任何代码——若联调暴露缺陷，只报告不修改。

运行: python scripts/scheduler_smoke.py
退出码: 0 = 至少 3 次触发且间隔 ~10s 且状态文件完整; 1 = 任一验证失败。
"""

import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.scheduler.task_scheduler import TaskScheduler  # noqa: E402

TICK_LOG = PROJECT_ROOT / 'data' / 'scheduler_smoke.log'
STATE_FILE = PROJECT_ROOT / 'data' / 'scheduler_state.json'
TASK_NAME = 'smoke_tick'
INTERVAL_SECONDS = 10
RUN_SECONDS = 35          # 10s 间隔 -> 期望 ~3 次触发（10s/20s/30s）
INTERVAL_TOLERANCE = 2.0  # 触发间隔允许 10±2s（APScheduler 有调度误差）

# 结果汇总在脚本结束时打印，同时写入本文件供交付报告引用
EVIDENCE_LINES = []


def note(msg: str) -> None:
    print(msg, flush=True)
    EVIDENCE_LINES.append(msg)


def smoke_tick(**payload) -> None:
    """冒烟 handler：只写日志行，无任何网络请求。

    data/scheduler_smoke.log 每行: <UTC ISO 时间>\t<单调计数>\t<payload>
    """
    seq = TICK_LOG_WRITE_COUNT[0]
    TICK_LOG_WRITE_COUNT[0] += 1
    line = (f"{datetime.now(timezone.utc).isoformat(timespec='milliseconds')}"
            f"\ttick#{seq}\t{json.dumps(payload, ensure_ascii=False)}")
    with open(TICK_LOG, 'a', encoding='utf-8') as f:
        f.write(line + '\n')
    print(f"[handler] {line}", flush=True)


TICK_LOG_WRITE_COUNT = [0]


def parse_tick_log() -> list:
    """从冒烟日志解析 (UTC datetime, 原始行) 列表。"""
    if not TICK_LOG.exists():
        return []
    out = []
    for raw in TICK_LOG.read_text(encoding='utf-8').splitlines():
        if '\ttick#' not in raw:
            continue
        ts_text = raw.split('\t', 1)[0]
        out.append((datetime.fromisoformat(ts_text), raw))
    return out


def main() -> int:
    # 清理上一次运行的冒烟产物，保证本次证据可归因
    if TICK_LOG.exists():
        TICK_LOG.unlink()
    if STATE_FILE.exists():
        STATE_FILE.unlink()

    note(f"=== scheduler smoke start {datetime.now(timezone.utc).isoformat()} ===")
    note(f"interval={INTERVAL_SECONDS}s run_seconds={RUN_SECONDS} "
         f"tick_log={TICK_LOG.name} state_file={STATE_FILE.name}")

    scheduler = TaskScheduler()  # 默认连 Redis；不可用会降级内存（下面会报告模式）
    queue_mode = 'memory(degraded)' if scheduler._queue.degraded else 'redis'
    note(f"queue_mode={queue_mode}")

    scheduler.register_handler(TASK_NAME, smoke_tick)
    task_id = scheduler.schedule_periodic(
        TASK_NAME, interval=INTERVAL_SECONDS, priority=5)
    note(f"registered task_id={task_id} interval={INTERVAL_SECONDS}s")

    consumed = 0
    started = time.monotonic()
    interrupted = False
    try:
        while time.monotonic() - started < RUN_SECONDS:
            # process_next: 队列空返回 False，到点有任务返回 True 并执行 handler
            if scheduler.process_next():
                consumed += 1
            time.sleep(0.2)
    except KeyboardInterrupt:
        interrupted = True
        note("KeyboardInterrupt received -> graceful shutdown path")

    note(f"main loop finished elapsed={time.monotonic() - started:.1f}s "
         f"interrupted={interrupted} process_next_consumed={consumed}")

    # 优雅退出
    scheduler.shutdown(wait=True)
    note("scheduler.shutdown(wait=True) done")

    # ---- 验证 1: 至少 3 次真实触发 ----
    ticks = parse_tick_log()
    note(f"tick_count={len(ticks)}")
    for _, raw in ticks:
        note(f"  tick_log: {raw}")

    intervals = []
    for prev, cur in zip(ticks, ticks[1:]):
        delta = (cur[0] - prev[0]).total_seconds()
        intervals.append(delta)
    ok_intervals = all(abs(d - INTERVAL_SECONDS) <= INTERVAL_TOLERANCE
                       for d in intervals)
    note(f"tick_intervals={[f'{d:.3f}s' for d in intervals]} "
         f"within_tolerance={ok_intervals}")

    # ---- 验证 2: process_next 消费与 handler 执行一一对应 ----
    # 入队次数 = 触发次数（每次周期触发 run_task 入队一次）
    ok_consumed = consumed >= len(ticks) and consumed > 0
    note(f"consumed_matches_ticks={ok_consumed} (consumed={consumed})")

    # ---- 验证 3: 状态文件完整可解析，且触发实例均为 completed ----
    state_ok = True
    try:
        state = json.loads(STATE_FILE.read_text(encoding='utf-8'))
        periodic = state.get(task_id, {})
        tick_entries = [v for v in state.values()
                        if v.get('task_name') == TASK_NAME and v.get('priority') == 5
                        and v.get('status') in ('completed',)]
        statuses = sorted({v.get('status') for v in state.values()})
        note(f"state_file_valid_json=True entries={len(state)} "
             f"periodic_entry={json.dumps(periodic, ensure_ascii=False)} "
             f"completed_task_entries={len(tick_entries)} statuses={statuses}")
        # 注: 状态文件不持久化 interval_seconds（只存在内存 _task_configs，
        # 见 task_scheduler.py:schedule_periodic），完整性按 status 口径校验。
        state_ok = (
            periodic.get('status') == 'scheduled'
            and len(tick_entries) >= len(ticks)
            and all(s in ('completed', 'scheduled') for s in statuses)
        )
    except Exception as exc:
        state_ok = False
        note(f"state_file_valid_json=False error={exc}")

    note("=== scheduler smoke end ===")

    passed = (
        len(ticks) >= 3
        and ok_intervals
        and ok_consumed
        and state_ok
    )
    note(f"RESULT={'PASS' if passed else 'FAIL'} "
         f"(ticks={len(ticks)}, intervals_ok={ok_intervals}, "
         f"consumed={consumed}, state_ok={state_ok})")
    return 0 if passed else 1


if __name__ == '__main__':
    # Ctrl+C（SIGINT）走 KeyboardInterrupt -> 优雅清理路径
    signal.signal(signal.SIGINT, signal.default_int_handler)
    sys.exit(main())
