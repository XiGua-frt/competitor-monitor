"""metrics 包：只读派生指标计算器（Agent D）。

公共导出：
    MetricsCalculator — 快照数据 → 业务指标（价格/BSR/评论/在售/BuyBox）。
"""

from src.metrics.calculator import MetricsCalculator

__all__ = ["MetricsCalculator"]
