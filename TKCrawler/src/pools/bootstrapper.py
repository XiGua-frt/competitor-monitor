"""Auto-Login Bootstrapper for batch account login and Cookie Pool import."""

import asyncio
import random
import uuid
from typing import Dict, List, Optional
from datetime import datetime
from pathlib import Path

try:
    from camoufox.async_api import AsyncCamoufox
    CAMOUFOX_AVAILABLE = True
except ImportError:
    CAMOUFOX_AVAILABLE = False

from .cookie_pool import CookiePoolManager
from .ip_pool import IPPoolManager
from ..common.logger import get_logger
from ..common.config import config

logger = get_logger(__name__)


class AutoLoginBootstrapper:
    """Batch login and import accounts to Cookie Pool."""
    
    # 抖音关键 Cookie 字段（27个核心字段）
    REQUIRED_COOKIES = [
        'sessionid',           # 最关键：会话ID
        'sessionid_ss',        # 会话签名
        'ttwid',               # TikTok Web ID
        '__ac_nonce',          # 防重放nonce
        '__ac_signature',      # 签名
        'msToken',             # 微软令牌
        's_v_web_id',          # 设备ID
        'passport_csrf_token', # CSRF令牌
        'passport_csrf_token_default',
        'sid_guard',           # 会话守卫
        'uid_tt',              # 用户ID
        'uid_tt_ss',           
        'sid_tt',              # 会话TT
        'tt_csrf_token',       # TT CSRF令牌
        'tt_chain_token',      # 链式令牌
        'odin_tt',             # Odin指纹
        'd_ticket',            # 设备票据
        'x-web-secsdk-uid',    # Web安全SDK UID
        'MONITOR_WEB_ID',      # 监控ID
        'download_guide',      # 下载引导
        'volume_info',         # 音量信息
        'store-idc',           # 存储IDC
        'store-country-code',  # 国家代码
        'xgplayer_user_id',    # 播放器用户ID
        'xgplayer_device_id',  # 播放器设备ID
        'tiktok_webapp_theme', # 主题
        'tiktok_webapp_theme_source',
    ]
    
    # 登录流程配置
    LOGIN_CONFIG = {
        'douyin_url': 'https://www.douyin.com/',
        'login_button_selector': 'text=登录',
        'phone_login_selector': 'text=手机号登录',
        'phone_input_selector': 'input[placeholder*="手机号"]',
        'password_input_selector': 'input[type="password"]',
        'submit_selector': 'button[type="submit"]',
        'captcha_wait_timeout': 30000,  # 30秒等待人工验证
        'post_login_delay': (30, 60),   # 登录后延迟30-60秒
    }
    
    def __init__(
        self,
        cookie_pool: CookiePoolManager,
        ip_pool: IPPoolManager,
        headless: bool = False,
        save_cookies_dir: Optional[Path] = None
    ):
        """Initialize bootstrapper.
        
        Args:
            cookie_pool: Cookie池管理器
            ip_pool: IP池管理器
            headless: 是否无头模式（建议False以便人工验证滑块）
            save_cookies_dir: Cookie保存目录（用于备份）
        """
        if not CAMOUFOX_AVAILABLE:
            raise ImportError(
                "camoufox not available. Install: pip install camoufox>=0.4.0"
            )
        
        self.cookie_pool = cookie_pool
        self.ip_pool = ip_pool
        self.headless = headless
        self.save_cookies_dir = save_cookies_dir or Path('./exports/cookies')
        self.save_cookies_dir.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"AutoLoginBootstrapper initialized (headless={headless})")
    
    async def batch_login(
        self,
        accounts: List[Dict[str, str]],
        max_concurrent: int = 1
    ) -> Dict[str, str]:
        """批量登录账号.
        
        Args:
            accounts: 账号列表 [{"phone": "138...", "password": "xxx"}, ...]
            max_concurrent: 最大并发数（建议1，避免触发风控）
        
        Returns:
            {"phone": "account_id", ...} 成功登录的映射
        """
        logger.info(f"Starting batch login for {len(accounts)} accounts")
        
        results = {}
        semaphore = asyncio.Semaphore(max_concurrent)
        
        async def _login_with_limit(account):
            async with semaphore:
                return await self._login_single_account(account)
        
        tasks = [_login_with_limit(acc) for acc in accounts]
        login_results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 整理结果
        for account, result in zip(accounts, login_results):
            phone = account.get('phone', 'unknown')
            if isinstance(result, Exception):
                logger.error(f"Account {phone} login failed: {result}")
            elif result:
                results[phone] = result
                logger.info(f"Account {phone} login succeeded -> {result}")
        
        logger.info(f"Batch login completed: {len(results)}/{len(accounts)} succeeded")
        return results
    
    async def _login_single_account(
        self,
        account: Dict[str, str]
    ) -> Optional[str]:
        """单个账号登录流程.
        
        Args:
            account: {"phone": "138...", "password": "xxx"}
        
        Returns:
            account_id 或 None（失败）
        """
        phone = account.get('phone', '')
        password = account.get('password', '')
        
        if not phone or not password:
            logger.error("Missing phone or password")
            return None
        
        logger.info(f"[{phone}] Starting login...")
        
        # 1. 选择可用代理
        proxy_id, proxy_url = await self._get_available_proxy()
        if not proxy_url:
            logger.warning(f"[{phone}] No available proxy, using direct connection")
        
        # 2. 启动浏览器并登录
        try:
            cookies = await self._perform_login(phone, password, proxy_url)
            if not cookies:
                return None
            
            # 3. 保存 cookies 备份
            await self._save_cookies_backup(phone, cookies)
            
            # 4. 导入到 Cookie Pool
            account_id = await self.import_to_pool(phone, cookies, proxy_id)
            
            # 5. 延迟（避免频繁登录触发风控）
            delay = random.uniform(*self.LOGIN_CONFIG['post_login_delay'])
            logger.info(f"[{phone}] Cooling down for {delay:.1f}s...")
            await asyncio.sleep(delay)
            
            return account_id
            
        except Exception as e:
            logger.error(f"[{phone}] Login failed: {e}", exc_info=True)
            return None
    
    async def _perform_login(
        self,
        phone: str,
        password: str,
        proxy_url: Optional[str]
    ) -> Optional[Dict[str, str]]:
        """执行浏览器登录流程.
        
        Args:
            phone: 手机号
            password: 密码
            proxy_url: 代理URL（可选）
        
        Returns:
            Cookie字典 或 None
        """
        browser_config = {
            'headless': self.headless,
            'humanize': True,  # 启用人类化行为
        }
        
        if proxy_url:
            browser_config['proxy'] = {'server': proxy_url}
        
        async with AsyncCamoufox(**browser_config) as browser:
            page = await browser.new_page()
            
            try:
                # Step 1: 打开抖音首页
                logger.info(f"[{phone}] Opening {self.LOGIN_CONFIG['douyin_url']}")
                await page.goto(self.LOGIN_CONFIG['douyin_url'], wait_until='networkidle')
                await asyncio.sleep(2)
                
                # Step 2: 点击登录按钮
                logger.info(f"[{phone}] Clicking login button")
                await page.click(self.LOGIN_CONFIG['login_button_selector'])
                await asyncio.sleep(1)
                
                # Step 3: 选择手机号登录
                logger.info(f"[{phone}] Selecting phone login")
                try:
                    await page.click(self.LOGIN_CONFIG['phone_login_selector'])
                    await asyncio.sleep(1)
                except Exception:
                    logger.warning(f"[{phone}] Phone login selector not found, may already be on login page")
                
                # Step 4: 输入手机号
                logger.info(f"[{phone}] Entering phone number")
                await page.fill(self.LOGIN_CONFIG['phone_input_selector'], phone)
                await asyncio.sleep(0.5)
                
                # Step 5: 输入密码
                logger.info(f"[{phone}] Entering password")
                await page.fill(self.LOGIN_CONFIG['password_input_selector'], password)
                await asyncio.sleep(0.5)
                
                # Step 6: 点击提交
                logger.info(f"[{phone}] Submitting login form")
                await page.click(self.LOGIN_CONFIG['submit_selector'])
                
                # Step 7: 等待滑块验证（人工完成）
                logger.warning(
                    f"[{phone}] ⚠️  Please complete CAPTCHA manually within "
                    f"{self.LOGIN_CONFIG['captcha_wait_timeout']/1000}s"
                )
                
                # 等待URL变化或特定元素出现（表示登录成功）
                try:
                    await page.wait_for_url(
                        '**/*',  # 等待任意URL变化
                        timeout=self.LOGIN_CONFIG['captcha_wait_timeout']
                    )
                except Exception:
                    logger.warning(f"[{phone}] URL did not change, checking for success indicators")
                
                await asyncio.sleep(3)
                
                # Step 8: 提取 cookies
                logger.info(f"[{phone}] Extracting cookies")
                cookies = await self._extract_cookies(page)
                
                if not cookies:
                    logger.error(f"[{phone}] Failed to extract cookies")
                    return None
                
                # 验证关键字段
                missing = [k for k in self.REQUIRED_COOKIES[:5] if k not in cookies]
                if missing:
                    logger.warning(f"[{phone}] Missing critical cookies: {missing}")
                    # 不阻断，部分字段可能延迟生成
                
                logger.info(f"[{phone}] Successfully extracted {len(cookies)} cookies")
                return cookies
                
            except Exception as e:
                logger.error(f"[{phone}] Browser operation failed: {e}", exc_info=True)
                return None
            finally:
                await page.close()
    
    async def _extract_cookies(self, page) -> Dict[str, str]:
        """从页面提取 cookies.
        
        Args:
            page: Playwright page对象
        
        Returns:
            Cookie字典 {name: value}
        """
        cookies_list = await page.context.cookies()
        cookies_dict = {}
        
        for cookie in cookies_list:
            name = cookie.get('name', '')
            value = cookie.get('value', '')
            
            # 过滤有效 cookie
            if name and value:
                cookies_dict[name] = value
        
        return cookies_dict
    
    async def _get_available_proxy(self) -> tuple[str, Optional[str]]:
        """获取可用代理.
        
        Returns:
            (proxy_id, proxy_url) 元组
        """
        try:
            proxy_node = self.ip_pool.get_proxy()
            if proxy_node:
                return proxy_node.proxy_id, proxy_node.proxy_url
        except Exception as e:
            logger.warning(f"Failed to get proxy: {e}")
        
        return '', None
    
    async def _save_cookies_backup(self, phone: str, cookies: Dict[str, str]):
        """保存 cookies 备份到文件.
        
        Args:
            phone: 手机号（作为文件名）
            cookies: Cookie字典
        """
        import json
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"{phone}_{timestamp}.json"
        filepath = self.save_cookies_dir / filename
        
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(
                    {
                        'phone': phone,
                        'timestamp': timestamp,
                        'cookies': cookies,
                        'cookie_count': len(cookies),
                    },
                    f,
                    indent=2,
                    ensure_ascii=False
                )
            logger.info(f"Cookies backup saved: {filepath}")
        except Exception as e:
            logger.error(f"Failed to save cookies backup: {e}")
    
    async def import_to_pool(
        self,
        phone: str,
        cookies: Dict[str, str],
        proxy_id: str = ''
    ) -> str:
        """导入账号到 Cookie Pool.
        
        Args:
            phone: 手机号（作为username）
            cookies: Cookie字典
            proxy_id: 绑定的代理ID
        
        Returns:
            account_id
        """
        # 如果没有指定代理，尝试自动分配
        if not proxy_id:
            try:
                proxy_node = self.ip_pool.get_proxy()
                if proxy_node:
                    proxy_id = proxy_node.proxy_id
                    logger.info(f"Auto-assigned proxy {proxy_id} to account {phone}")
            except Exception as e:
                logger.warning(f"Failed to auto-assign proxy: {e}")
        
        # 添加到 Cookie Pool
        account_id = self.cookie_pool.add_account(
            username=phone,
            cookies=cookies,
            proxy_id=proxy_id or '',
            daily_quota=config.DAILY_QUOTA_PRODUCTS  # 使用配置的默认配额
        )
        
        logger.info(f"Account {phone} imported to pool: {account_id}")
        return account_id
    
    async def verify_login_status(self, account_id: str) -> bool:
        """验证账号登录状态（可选的健康检查）.
        
        Args:
            account_id: 账号ID
        
        Returns:
            True if logged in and valid
        """
        try:
            account = self.cookie_pool._load_account(account_id)
            if not account:
                return False
            
            # TODO: 实现真实的登录状态检查（发送测试请求）
            # 这里简化为检查关键 cookie 是否存在
            has_sessionid = 'sessionid' in account.cookies
            has_ttwid = 'ttwid' in account.cookies
            
            is_valid = has_sessionid and has_ttwid
            logger.info(f"Account {account.username} login status: {is_valid}")
            
            return is_valid
            
        except Exception as e:
            logger.error(f"Failed to verify login status: {e}")
            return False


# 便捷函数
async def bootstrap_accounts_from_file(
    filepath: Path,
    cookie_pool: CookiePoolManager,
    ip_pool: IPPoolManager,
    headless: bool = False
) -> Dict[str, str]:
    """从文件批量导入账号.
    
    Args:
        filepath: JSON文件路径，格式: [{"phone": "138...", "password": "xxx"}, ...]
        cookie_pool: Cookie池
        ip_pool: IP池
        headless: 是否无头模式
    
    Returns:
        {"phone": "account_id", ...}
    """
    import json
    
    with open(filepath, 'r', encoding='utf-8') as f:
        accounts = json.load(f)
    
    bootstrapper = AutoLoginBootstrapper(
        cookie_pool=cookie_pool,
        ip_pool=ip_pool,
        headless=headless
    )
    
    results = await bootstrapper.batch_login(accounts)
    return results
