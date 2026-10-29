"""TKCrawler 派生指标模块 (Agent I)。

对外导出:
    - MetricsCalculator: 4 个派生指标计算器 (calculator.py)
    - sql_views.sql: PostgreSQL 视图 DDL (product_metrics / views_growth, 待 PG 实测)
"""

from src.metrics.calculator import MetricsCalculator

__all__ = ["MetricsCalculator"]
