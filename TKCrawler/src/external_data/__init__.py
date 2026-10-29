"""External data integration layer.

第三方数据源接入:
    - alibaba_client.AlibabaClient  1688 开放平台 (供应商/采购价/MOQ/交期)
    - trends_client.TrendsClient    Google Trends (pytrends)
    - amazon_client.AmazonClient    Amazon PA-API v5 (BSR)
    - enricher.ExternalDataEnricher 主入口, 合并以上数据源

所有客户端凭据可选: 凭据缺失时进入"未配置"状态, 查询方法返回 None
并记录 warning, 绝不在无凭据时发起网络请求或抛出异常。
"""

from .enricher import ExternalDataEnricher
from .alibaba_client import AlibabaClient
from .trends_client import TrendsClient
from .amazon_client import AmazonClient

__all__ = [
    'ExternalDataEnricher',
    'AlibabaClient',
    'TrendsClient',
    'AmazonClient',
]
