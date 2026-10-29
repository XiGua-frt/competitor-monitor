"""scheduler 包：周期调度 + 任务队列 + 三个内置 job（Agent D）。

公共导出：
    TaskScheduler, Task, RetryPolicy, parse_interval
    job_scrape_watchlist / job_scrape_keywords / job_scrape_offers
    start_with_handlers — 一键注册全部 job
"""

from src.scheduler.jobs import (
    job_scrape_keywords,
    job_scrape_offers,
    job_scrape_watchlist,
    start_with_handlers,
)
from src.scheduler.task_scheduler import (
    RetryPolicy,
    Task,
    TaskScheduler,
    parse_interval,
)

__all__ = [
    "TaskScheduler",
    "Task",
    "RetryPolicy",
    "parse_interval",
    "job_scrape_watchlist",
    "job_scrape_keywords",
    "job_scrape_offers",
    "start_with_handlers",
]
