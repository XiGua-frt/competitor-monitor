"""Google Trends 客户端 (pytrends): 关键词热度得分 0-1 归一化。

无需凭据, 但测试一律 mock pytrends 响应 (网络沙箱不稳定, 不发真实请求)。

得分定义: 最近 timeframe 窗口内 interest_over_time 均值 / 100, 截断到
[0.0, 1.0]。查询失败或无数据时返回 -1.0 (明确无效值, 调用方据此跳过),
并记录 warning。
"""

import os
from typing import Optional

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from src.common.logger import get_logger

logger = get_logger('external_data.trends')

TRENDS_UNAVAILABLE = -1.0


class TrendsClient:
    """Google Trends 关键词热度客户端。"""

    def __init__(
        self,
        timeframe: Optional[str] = None,
        geo: Optional[str] = None,
        timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
        retry_wait: Optional[float] = None,
    ):
        self.timeframe = timeframe or os.getenv('EXTERNAL_TRENDS_TIMEFRAME', 'today 3-m')
        self.geo = geo if geo is not None else os.getenv('EXTERNAL_TRENDS_GEO', '')
        self.timeout = timeout if timeout is not None else int(os.getenv('EXTERNAL_TIMEOUT', '10'))
        max_retries = max_retries if max_retries is not None else int(os.getenv('EXTERNAL_MAX_RETRIES', '3'))
        retry_wait = retry_wait if retry_wait is not None else float(os.getenv('EXTERNAL_RETRY_WAIT_SECONDS', '2'))

        # 每个实例独立的重试策略: 网络沙箱下 pytrends 查询可能瞬时失败
        self._query_with_retry = retry(
            retry=retry_if_exception_type(Exception),
            stop=stop_after_attempt(max(1, max_retries)),
            wait=wait_fixed(retry_wait),
            reraise=True,
        )(self._query)

    # ------------------------------------------------------------------
    def _build_trend(self):
        """惰性创建 pytrends TrendReq (便于测试替换/打桩)。"""
        from pytrends.request import TrendReq
        return TrendReq(hl='en-US', tz=0, timeout=self.timeout)

    def _query(self, keyword: str) -> float:
        """执行 pytrends 查询, 返回 interest 均值 / 100 的原始得分。"""
        trend = self._build_trend()
        trend.build_payload([keyword], timeframe=self.timeframe, geo=self.geo)
        df = trend.interest_over_time()
        if df is None or df.empty:
            raise ValueError(f'Google Trends 无数据: keyword={keyword!r}')
        series = df[keyword] if keyword in df.columns else df.iloc[:, 0]
        return float(series.mean()) / 100.0

    # ------------------------------------------------------------------
    def get_trends_score(self, keyword: str) -> float:
        """返回关键词热度得分 (0.0-1.0); 失败/无数据返回 -1.0。"""
        try:
            raw = self._query_with_retry(keyword)
            return round(max(0.0, min(1.0, raw)), 4)
        except Exception as exc:  # pytrends 异常类型不稳定, 统一兜底
            logger.warning(f'Google Trends 查询失败: keyword={keyword!r}, error={exc}')
            return TRENDS_UNAVAILABLE
