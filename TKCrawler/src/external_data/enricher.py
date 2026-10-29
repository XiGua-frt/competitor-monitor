"""ExternalDataEnricher: 外部数据富化主入口。

将 1688 供应商信息 / Google Trends 得分 / Amazon BSR 合并进商品 dict。
任一数据源失败 (未配置、请求异常、无数据) 均不阻塞整体:
降级返回部分结果并记录 warning。

输出键命名规范 (新增键不覆盖 product_data 已有键):
    - supplier_name / supplier_price / supplier_moq /
      supplier_lead_time_days / supplier_url   -> 1688 供应商信息
    - trends_score                             -> Google Trends 得分 (0-1)
    - amazon_rank / amazon_category / amazon_asin -> Amazon BSR
"""

from typing import Optional

from src.common.logger import get_logger

from .alibaba_client import AlibabaClient
from .amazon_client import AmazonClient
from .trends_client import TRENDS_UNAVAILABLE, TrendsClient

logger = get_logger('external_data.enricher')


class ExternalDataEnricher:
    """商品外部数据富化器。"""

    def __init__(
        self,
        alibaba_client: Optional[AlibabaClient] = None,
        trends_client: Optional[TrendsClient] = None,
        amazon_client: Optional[AmazonClient] = None,
    ):
        self.alibaba = alibaba_client or AlibabaClient()
        self.trends = trends_client or TrendsClient()
        self.amazon = amazon_client or AmazonClient()

    # ------------------------------------------------------------------
    def get_supplier_info(self, product_title: str) -> dict:
        """1688 搜索: 采购价/MOQ/交期/供应商名。

        返回 dict (可为空 dict); 未配置或失败返回 {}。
        """
        try:
            return self.alibaba.get_supplier_info(product_title) or {}
        except Exception as exc:
            logger.warning(
                f'1688 供应商查询异常: title={product_title!r}, error={exc}')
            return {}

    def get_trends_score(self, keyword: str) -> float:
        """Google Trends 得分 (0.0-1.0); 失败返回 -1.0。"""
        try:
            return self.trends.get_trends_score(keyword)
        except Exception as exc:
            logger.warning(f'Trends 查询异常: keyword={keyword!r}, error={exc}')
            return TRENDS_UNAVAILABLE

    def get_amazon_bsr(self, asin: str) -> dict:
        """Amazon BSR 查询: {'rank', 'category', 'asin'}。

        未配置或失败返回 {}。
        """
        try:
            return self.amazon.get_amazon_bsr(asin) or {}
        except Exception as exc:
            logger.warning(f'Amazon BSR 查询异常: asin={asin!r}, error={exc}')
            return {}

    # ------------------------------------------------------------------
    def enrich_product(self, product_data: dict) -> dict:
        """合并外部数据到商品 dict (返回浅拷贝, 不修改入参, 不覆盖已有键)。

        数据源与所需字段:
            - 1688: 依据 product_data['title']
            - Trends: 依据 product_data['keyword'] (缺省用 title)
            - Amazon BSR: 依据 product_data['asin'] (缺失则跳过)
        """
        enriched = dict(product_data)
        title = enriched.get('title') or ''

        # --- 1688 供应商 ---
        if title:
            supplier = self.get_supplier_info(title)
            self._merge_prefix(enriched, supplier, 'supplier_')
        else:
            logger.warning('商品缺少 title, 跳过 1688 供应商查询')

        # --- Google Trends ---
        keyword = enriched.get('keyword') or title
        if keyword:
            score = self.get_trends_score(keyword)
            if score != TRENDS_UNAVAILABLE and 'trends_score' not in enriched:
                enriched['trends_score'] = score
        else:
            logger.warning('商品缺少 keyword, 跳过 Trends 查询')

        # --- Amazon BSR ---
        asin = enriched.get('asin')
        if asin:
            bsr = self.get_amazon_bsr(asin)
            for source_key, target_key in (
                ('rank', 'amazon_rank'),
                ('category', 'amazon_category'),
                ('asin', 'amazon_asin'),
            ):
                if source_key in bsr and target_key not in enriched:
                    enriched[target_key] = bsr[source_key]
        else:
            logger.debug('商品缺少 asin, 跳过 Amazon BSR 查询')

        return enriched

    # ------------------------------------------------------------------
    @staticmethod
    def _merge_prefix(target: dict, source: dict, prefix: str) -> None:
        """将 source 键加统一前缀后合并, 不覆盖 target 已有键。

        source 键已带前缀 (如 supplier_) 时不再重复添加。
        """
        for key, value in source.items():
            new_key = key if key.startswith(prefix) else f'{prefix}{key}'
            if new_key not in target:
                target[new_key] = value
