"""Amazon PA-API v5 客户端: 按 ASIN 查询 BSR (类目排名)。

凭据 (Access Key / Secret Key / Partner Tag) 可选:
    - 环境变量: EXTERNAL_AMAZON_ACCESS_KEY / EXTERNAL_AMAZON_SECRET_KEY /
      EXTERNAL_AMAZON_PARTNER_TAG
    - 凭据缺失时 is_configured 为 False, get_amazon_bsr 返回 None 并
      log warning, 不发起任何网络请求。

实现说明: requirements.txt 中的 amazon-paapi 包在当前环境无法安装
(沙箱网络限制), 故用 requests + 手写 SigV4 签名直接调用 PA-API v5
GetItems 接口。请求/响应结构【待凭据就绪后实测】。
"""

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from typing import Optional

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from src.common.logger import get_logger

logger = get_logger('external_data.amazon')

SERVICE = 'ProductAdvertisingAPI'
CONTENT_TYPE = 'application/json; charset=utf-8'
AMZ_TARGET_PREFIX = 'com.amazon.paapi5.v1.ProductAdvertisingAPIv1.GetItems'


class AmazonClient:
    """Amazon PA-API v5 BSR 查询客户端。"""

    def __init__(
        self,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        partner_tag: Optional[str] = None,
        host: Optional[str] = None,
        region: Optional[str] = None,
        marketplace: Optional[str] = None,
        timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
        retry_wait: Optional[float] = None,
    ):
        self.access_key = access_key or os.getenv('EXTERNAL_AMAZON_ACCESS_KEY')
        self.secret_key = secret_key or os.getenv('EXTERNAL_AMAZON_SECRET_KEY')
        self.partner_tag = partner_tag or os.getenv('EXTERNAL_AMAZON_PARTNER_TAG')
        self.host = host or os.getenv('EXTERNAL_AMAZON_HOST', 'webservices.amazon.com')
        self.region = region or os.getenv('EXTERNAL_AMAZON_REGION', 'us-east-1')
        self.marketplace = marketplace or os.getenv('EXTERNAL_AMAZON_MARKETPLACE', 'www.amazon.com')
        self.timeout = timeout if timeout is not None else int(os.getenv('EXTERNAL_TIMEOUT', '10'))
        max_retries = max_retries if max_retries is not None else int(os.getenv('EXTERNAL_MAX_RETRIES', '3'))
        retry_wait = retry_wait if retry_wait is not None else float(os.getenv('EXTERNAL_RETRY_WAIT_SECONDS', '2'))

        self._request_with_retry = retry(
            retry=retry_if_exception_type(requests.RequestException),
            stop=stop_after_attempt(max(1, max_retries)),
            wait=wait_fixed(retry_wait),
            reraise=True,
        )(self._request)

    # ------------------------------------------------------------------
    @property
    def is_configured(self) -> bool:
        """凭据是否就绪。"""
        return bool(self.access_key and self.secret_key and self.partner_tag)

    # ------------------------------------------------------------------
    # SigV4 签名 (PA-API v5 要求)
    # ------------------------------------------------------------------
    def _sign_request(self, amz_date: str, date_stamp: str, payload: bytes) -> dict:
        """构造带 Authorization 头的请求头。"""
        endpoint = f'https://{self.host}/paapi5/getitems'
        canonical_uri = '/paapi5/getitems'
        canonical_query = ''
        canonical_headers = (
            f'content-encoding:amz-1.0\n'
            f'content-type:{CONTENT_TYPE}\n'
            f'host:{self.host}\n'
            f'x-amz-date:{amz_date}\n'
            f'x-amz-target:{AMZ_TARGET_PREFIX}\n'
        )
        signed_headers = 'content-encoding;content-type;host;x-amz-date;x-amz-target'
        payload_hash = hashlib.sha256(payload).hexdigest()

        canonical_request = '\n'.join([
            'POST', canonical_uri, canonical_query, canonical_headers,
            signed_headers, payload_hash,
        ])
        credential_scope = f'{date_stamp}/{self.region}/{SERVICE}/aws4_request'
        string_to_sign = '\n'.join([
            'AWS4-HMAC-SHA256', amz_date, credential_scope,
            hashlib.sha256(canonical_request.encode('utf-8')).hexdigest(),
        ])

        def _hmac(key: bytes, msg: str) -> bytes:
            return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()

        k_date = _hmac(('AWS4' + self.secret_key).encode('utf-8'), date_stamp)
        k_region = _hmac(k_date, self.region)
        k_service = _hmac(k_region, SERVICE)
        k_signing = _hmac(k_service, 'aws4_request')
        signature = hmac.new(
            k_signing, string_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()

        return {
            'content-encoding': 'amz-1.0',
            'content-type': CONTENT_TYPE,
            'x-amz-date': amz_date,
            'x-amz-target': AMZ_TARGET_PREFIX,
            'authorization': (
                f'AWS4-HMAC-SHA256 Credential={self.access_key}/{credential_scope}, '
                f'SignedHeaders={signed_headers}, Signature={signature}'
            ),
        }

    # ------------------------------------------------------------------
    def _request(self, payload: bytes, headers: dict) -> dict:
        url = f'https://{self.host}/paapi5/getitems'
        resp = requests.post(url, data=payload, headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    def get_amazon_bsr(self, asin: str) -> Optional[dict]:
        """按 ASIN 查询 BSR。

        返回 {'rank': int, 'category': str, 'asin': str}; 无 BSR 数据返回
        空 dict; 未配置/请求失败返回 None。
        """
        if not self.is_configured:
            logger.warning(
                'Amazon PA-API 凭据未配置 (EXTERNAL_AMAZON_ACCESS_KEY / '
                'EXTERNAL_AMAZON_SECRET_KEY / EXTERNAL_AMAZON_PARTNER_TAG), '
                '跳过 BSR 查询')
            return None

        body = {
            'PartnerTag': self.partner_tag,
            'PartnerType': 'Associates',
            'Marketplace': self.marketplace,
            'ItemIds': [asin],
            'Resources': [
                'BrowseNodeInfo.BrowseNodes.SalesRank',
                'BrowseNodeInfo.BrowseNodes.DisplayName',
            ],
        }
        now = datetime.now(timezone.utc)
        amz_date = now.strftime('%Y%m%dT%H%M%SZ')
        date_stamp = now.strftime('%Y%m%d')
        payload = json.dumps(body).encode('utf-8')
        headers = self._sign_request(amz_date, date_stamp, payload)

        try:
            data = self._request_with_retry(payload, headers)
        except requests.RequestException as exc:
            logger.warning(f'Amazon PA-API 请求失败 (asin={asin}): {exc}')
            return None
        except ValueError as exc:
            logger.warning(f'Amazon PA-API 响应非 JSON (asin={asin}): {exc}')
            return None

        if 'Errors' in data:
            logger.warning(f'Amazon PA-API 返回错误 (asin={asin}): {data["Errors"]}')
            return None
        return self._parse_bsr(data, asin)

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_bsr(data: dict, asin: str) -> dict:
        """从 GetItems 响应解析首个带 SalesRank 的类目节点。"""
        items = (data.get('ItemsResult') or {}).get('Items') or []
        if not items:
            return {}
        nodes = ((items[0].get('BrowseNodeInfo') or {}).get('BrowseNodes')) or []
        for node in nodes:
            if node.get('SalesRank') is not None:
                return {
                    'rank': node['SalesRank'],
                    'category': node.get('DisplayName') or '',
                    'asin': asin,
                }
        return {}
