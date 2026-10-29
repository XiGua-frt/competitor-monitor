"""MetricsService — 指标查询的业务入口（包装 MetricsCalculator，只读）。"""

from typing import List

from src.metrics.calculator import MetricsCalculator

try:
    from src.common.logger import get_logger
    logger = get_logger(__name__)
except ImportError:  # pragma: no cover
    import logging
    logger = logging.getLogger(__name__)


class MetricsService:
    """面向 CLI/MCP 的指标查询薄封装（全部只读，None = 不可计算）。"""

    def __init__(self, calculator: MetricsCalculator = None):
        self._calc = calculator  # 惰性构造（避免 import 时建连接）

    @property
    def calculator(self) -> MetricsCalculator:
        if self._calc is None:
            self._calc = MetricsCalculator()
        return self._calc

    def asin_report(self, asin: str, days: int = 30) -> dict:
        """单 ASIN 全量指标报告（price/bsr/review/downtime/buybox）。"""
        return self.calculator.asin_report(asin, days)

    def list_reports(self, days: int = 30, sort_by: str = "review_velocity") -> list:
        """watchlist 全部 ASIN 的报告列表，按指定字段降序排序。

        sort_by 取值: review_velocity / price_change / bsr_change /
        availability_downtime / buybox_flips；None 值排在最后。
        """
        reports = self._reports_for_all(days)
        return self._sort_reports(reports, sort_by)

    def find_surging(self, min_review_velocity: float = 5.0,
                     window_days: int = 7) -> List[dict]:
        """窗口内评论增速 >= 阈值的 ASIN 列表（降序）。"""
        return self.calculator.surging_products(
            min_review_velocity=min_review_velocity, window_days=window_days)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _reports_for_all(self, days: int) -> list:
        """扫描库内全部 distinct ASIN 生成报告。"""
        try:
            from sqlalchemy import select
            from src.database.models import AsinSnapshot
            with self.calculator._session_factory() as session:
                asins = session.execute(
                    select(AsinSnapshot.asin).distinct()).scalars().all()
            return [self.asin_report(asin, days) for asin in asins]
        except Exception:
            logger.exception("list_reports: 扫描 ASIN 失败")
            return []

    @staticmethod
    def _sort_reports(reports: list, sort_by: str) -> list:
        """按指标降序；None 值沉底（不可计算的排最后）。"""
        key_map = {
            "review_velocity": lambda r: r.get("review_velocity"),
            "price_change": lambda r: (r.get("price_change") or {}).get("change"),
            "bsr_change": lambda r: (r.get("bsr_change") or {}).get("change"),
            "availability_downtime": lambda r: r.get("availability_downtime"),
            "buybox_flips": lambda r: r.get("buybox_flips"),
        }
        key_fn = key_map.get(sort_by, key_map["review_velocity"])
        # None 沉底：分 (is_not_none, value) 元组；None 值统一排最后组内不比较
        def sort_key(r):
            v = key_fn(r)
            if v is None:
                return (0, 0)
            try:
                return (1, float(v))
            except (TypeError, ValueError):
                return (0, 0)
        return sorted(reports, key=sort_key, reverse=True)
