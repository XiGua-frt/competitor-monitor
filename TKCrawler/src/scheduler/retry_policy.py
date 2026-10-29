"""指数退避重试策略：默认 1m → 2m → 4m，max_attempts=3，均可配置。"""

from typing import List, Optional

from src.common.config import config
from src.common.logger import get_logger

logger = get_logger("scheduler.retry_policy")


class RetryPolicy:
    """任务失败后的重试策略。

    - exponential 模式：delay = base_delay * backoff_factor ** (attempt - 1)
      默认 base_delay=60s、factor=2 → 第1次重试 60s，第2次 120s，第3次 240s。
    - fixed 模式：每次重试间隔固定为 base_delay。
    """

    def __init__(
        self,
        max_attempts: Optional[int] = None,
        base_delay: Optional[int] = None,
        backoff_factor: float = 2,
        mode: str = "exponential",
        max_delay: Optional[int] = None,
    ):
        if mode not in ("exponential", "fixed"):
            raise ValueError(f"未知的重试模式: {mode}（可选 exponential / fixed）")
        self.max_attempts = max_attempts if max_attempts is not None else config.MAX_RETRY_ATTEMPTS
        self.base_delay = base_delay if base_delay is not None else config.RETRY_BACKOFF_BASE
        self.backoff_factor = backoff_factor
        self.mode = mode
        self.max_delay = max_delay

    def get_delay(self, attempt: int) -> int:
        """计算第 attempt 次重试前的等待秒数（attempt 从 1 开始）。"""
        if attempt < 1:
            raise ValueError(f"attempt 必须 >= 1，收到: {attempt}")
        if self.mode == "fixed":
            delay = self.base_delay
        else:
            delay = self.base_delay * (self.backoff_factor ** (attempt - 1))
        if self.max_delay is not None:
            delay = min(delay, self.max_delay)
        return int(delay)

    def should_retry(self, attempts: int) -> bool:
        """已失败 attempts 次后是否还应重试。"""
        return attempts < self.max_attempts

    def retry_sequence(self) -> List[int]:
        """完整重试间隔序列（秒），便于测试与日志展示。"""
        return [self.get_delay(i) for i in range(1, self.max_attempts + 1)]
