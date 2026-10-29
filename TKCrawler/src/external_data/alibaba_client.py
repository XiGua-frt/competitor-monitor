"""1688 开放平台客户端: 供应商信息搜索 (采购价/MOQ/交期/供应商名)。

凭据 (App Key/Secret) 可选:
    - 读取顺序: 环境变量 EXTERNAL_ALIBABA_APP_KEY / EXTERNAL_ALIBABA_APP_SECRET
      -> src/common/config.py 的 ALIBABA_APP_KEY / ALIBABA_APP_SECRET
    - 凭据缺失时 is_configured 为 False, get_supplier_info 返回 None 并
      log warning, 不发起任何网络请求。

签名方式【假设】: 按 1688 开放平台通用规范, 将 url_path 与按 key 排序的
请求参数 (key + value 依次拼接) 合并后用 App Secret 做 HMAC-SHA256,
签名放在参数 _aop_signature 中。真实字段结构【待凭据就绪后实测】。
"""

import hashlib
import hmac
import os
from typing import Optional

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from src.common.config import config
from src.common.logger import get_logger

logger = get_logger('external_data.alibaba')

GATEWAY_HOST = 'https://gw.open.1688.com'
# 1688 开放平台"商品搜索"接口【假设的 namespace/name, 待实测确认】
API_NAMESPACE = 'com.alibaba.linkplus'
API_NAME = 'alibaba.linkplus.offer.search'
# OAuth token 端点 namespace (1688 开放平台标准)
OAUTH_NAMESPACE = 'system.oauth2'


class AlibabaClient:
    """1688 开放平台供应商搜索客户端。"""

    def __init__(
        self,
        app_key: Optional[str] = None,
        app_secret: Optional[str] = None,
        timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
        retry_wait: Optional[float] = None,
    ):
        self.app_key = app_key or os.getenv('EXTERNAL_ALIBABA_APP_KEY') or config.ALIBABA_APP_KEY
        self.app_secret = app_secret or os.getenv('EXTERNAL_ALIBABA_APP_SECRET') or config.ALIBABA_APP_SECRET
        self.timeout = timeout if timeout is not None else int(os.getenv('EXTERNAL_TIMEOUT', '10'))
        max_retries = max_retries if max_retries is not None else int(os.getenv('EXTERNAL_MAX_RETRIES', '3'))
        retry_wait = retry_wait if retry_wait is not None else float(os.getenv('EXTERNAL_RETRY_WAIT_SECONDS', '2'))

        # 每个实例独立的重试策略: 仅对网络异常重试, 重试耗尽后向上抛出
        self._request_with_retry = retry(
            retry=retry_if_exception_type(requests.RequestException),
            stop=stop_after_attempt(max(1, max_retries)),
            wait=wait_fixed(retry_wait),
            reraise=True,
        )(self._request)

        self._access_token: Optional[str] = None

    # ------------------------------------------------------------------
    @property
    def is_configured(self) -> bool:
        """凭据是否就绪。"""
        return bool(self.app_key and self.app_secret)

    # ------------------------------------------------------------------
    def _api_url_path(self, api_name: str, namespace: str = API_NAMESPACE) -> str:
        """1688 网关 API 路径 (参与签名)。"""
        return f'param2/1/{namespace}/{api_name}/{self.app_key}'

    def _sign(self, api_name: str, params: dict, namespace: str = API_NAMESPACE) -> str:
        """HMAC-SHA256 签名: url_path + 按 key 排序的 key+value 拼接。"""
        normalized = {k: '' if v is None else str(v) for k, v in params.items()}
        concat = self._api_url_path(api_name, namespace) + ''.join(
            f'{k}{normalized[k]}' for k in sorted(normalized))
        return hmac.new(
            self.app_secret.encode('utf-8'), concat.encode('utf-8'), hashlib.sha256
        ).hexdigest().upper()

    def _request(self, url: str, params: dict) -> dict:
        resp = requests.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def _signed_call(self, api_name: str, params: dict,
                     namespace: str = API_NAMESPACE) -> Optional[dict]:
        """带签名的网关调用, 返回 JSON dict; 失败返回 None。"""
        if not self.is_configured:
            return None
        url = f'{GATEWAY_HOST}/openapi/{self._api_url_path(api_name, namespace)}'
        params = dict(params)
        params['_aop_signature'] = self._sign(api_name, params, namespace)
        try:
            return self._request_with_retry(url, params)
        except requests.RequestException as exc:
            logger.warning(f'1688 网关请求失败 (api={api_name}): {exc}')
            return None
        except ValueError as exc:  # JSON 解析失败
            logger.warning(f'1688 网关响应非 JSON (api={api_name}): {exc}')
            return None

    # ------------------------------------------------------------------
    def _ensure_access_token(self) -> str:
        """获取 OAuth access token【假设使用 system.oauth2 getToken 端点】。"""
        if self._access_token:
            return self._access_token
        data = self._signed_call(
            'getToken',
            {
                'grant_type': 'client_credentials',
                'client_id': self.app_key,
                'client_secret': self.app_secret,
            },
            namespace=OAUTH_NAMESPACE,
        ) or {}
        self._access_token = data.get('access_token') or ''
        return self._access_token

    # ------------------------------------------------------------------
    def get_supplier_info(self, product_title: str) -> Optional[dict]:
        """按商品标题搜索 1688 供应商。

        返回 dict (空 dict 表示搜索无结果) 或 None (未配置/请求失败)。
        """
        if not self.is_configured:
            logger.warning(
                '1688 App Key/Secret 未配置 (EXTERNAL_ALIBABA_APP_KEY / '
                'EXTERNAL_ALIBABA_APP_SECRET), 跳过供应商查询')
            return None

        token = self._ensure_access_token()
        data = self._signed_call(API_NAME, {
            'access_token': token,
            'keywords': product_title,
            'pageNo': 1,
            'pageSize': 5,
        })
        if data is None:
            return None
        return self._parse_first_offer(data)

    # ------------------------------------------------------------------
    @staticmethod
    def _extract_offer_list(data: dict) -> list:
        """从响应中提取 offer 列表【兼容两种假设的响应结构】。"""
        result = data.get('result', data)
        if isinstance(result, dict):
            result = result.get('list') or result.get('offers') or []
        return result if isinstance(result, list) else []

    @classmethod
    def _parse_first_offer(cls, data: dict) -> dict:
        offers = cls._extract_offer_list(data)
        if not offers:
            return {}
        offer = offers[0] or {}
        price_info = offer.get('priceInfo') or offer.get('price') or {}
        if isinstance(price_info, list):  # 区间价取最低档
            price_info = price_info[0] if price_info else {}
        supplier = offer.get('supplier') or offer.get('sellerInfo') or {}
        return {
            'supplier_name': supplier.get('companyName') or offer.get('companyName') or '',
            'supplier_price': price_info.get('price') or offer.get('price'),
            'supplier_moq': offer.get('minOrderQuantity') or offer.get('moq'),
            'supplier_lead_time_days': offer.get('leadTime') or offer.get('deliveryTime'),
            'supplier_url': offer.get('offerUrl') or offer.get('detailUrl') or '',
        }
