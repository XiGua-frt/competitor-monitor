#!/usr/bin/env python3
"""把浏览器登录捕获的 TikTok cookie 导入 Cookie 池.

两种用法:

1. 登录 + 导入一步到位 (必须在 Terminal 运行, WorkBuddy 沙箱内浏览器会挂死):
   .venv/bin/python scripts/pool_add_account.py --label acc01

2. 从已有 session 文件导入 (不弹浏览器, 可在任意环境运行):
   .venv/bin/python scripts/pool_add_account.py --label acc01 --from-file data/session.json

登录捕获逻辑与 scripts/login_tiktok.py 完全一致 (camoufox 有界面 + 人工登录 +
轮询 sessionid); 导入后自动绑定代理 (默认本地干净出口 127.0.0.1:7892)。

退出码: 0 成功 / 1 登录超时或导入失败 / 2 环境错误 (Redis 不可用等)
"""
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.common.config import config  # noqa: E402
from src.common.logger import get_logger  # noqa: E402
from src.pools.cookie_pool import CookiePoolManager  # noqa: E402
from src.pools.ip_pool import IPPoolManager  # noqa: E402

logger = get_logger("pool_add_account")

SESSION_FILE = PROJECT_ROOT / 'data' / 'session.json'
SESSIONS_DIR = PROJECT_ROOT / 'data' / 'sessions'
LOGIN_URL = 'https://www.tiktok.com/login/phone-or-email/email'
POLL_INTERVAL = 2
TIMEOUT = 480
DEFAULT_PROXY = 'http://127.0.0.1:7892'
REQUIRED_COOKIES = ('sessionid', 'ttwid')  # 池系统校验登录态的最低要求
LOGIN_COOKIES = ('sessionid', 'sessionid_ss')  # 轮询登录态的标志


async def capture_cookies_via_browser(proxy: str) -> dict | None:
    """弹出浏览器人工登录, 轮询捕获 cookie (逻辑同 scripts/login_tiktok.py)."""
    proxy_cfg = {'server': proxy} if proxy else None

    try:
        from camoufox.async_api import AsyncCamoufox
        launcher = 'camoufox'
    except ImportError:
        AsyncCamoufox = None
        launcher = 'playwright-firefox'

    print(f'[*] 启动浏览器: {launcher} (有界面模式)')
    print(f'[*] 登录页: {LOGIN_URL}')
    print('[*] 请在弹出的浏览器窗口中完成登录 (含验证码)')
    print(f'[*] 超时 {TIMEOUT}s\n')

    if AsyncCamoufox is not None:
        cm = AsyncCamoufox(headless=False, proxy=proxy_cfg,
                           i_know_what_im_doing=True)
    else:
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        cm = pw.firefox.launch(headless=False, proxy=proxy_cfg)

    browser = await cm.__aenter__()
    context = await browser.new_context()
    page = await context.new_page()
    # TikTok 前端会做客户端重定向循环，domcontentloaded 可能永远等不到；
    # commit 即视为导航开始。导航失败不退出——轮询只看 cookie，
    # 用户可在窗口中手动操作（含地址栏直接输入 www.tiktok.com 登录）。
    try:
        await page.goto(LOGIN_URL, wait_until='commit', timeout=90_000)
        print('[*] 页面导航已开始, 开始轮询登录态...')
    except Exception as e:
        print(f'[!] 自动打开登录页失败: {type(e).__name__}')
        print('[!] 不影响使用: 请在浏览器窗口地址栏手动输入 www.tiktok.com 并登录,')
        print('[!] 脚本会持续轮询 cookie, 检测到登录态自动入池。')

    jar = None
    try:
        deadline = time.time() + TIMEOUT
        while time.time() < deadline:
            try:
                raw = await context.cookies('https://www.tiktok.com')
            except Exception as e:
                print(f'\n[!] cookie 轮询异常: {e}')
                break
            found = {c['name']: c['value'] for c in raw}
            if any(name in found for name in LOGIN_COOKIES):
                jar = found
                break
            remaining = int(deadline - time.time())
            print(f'\r[.] 等待登录完成... 剩余 {remaining}s '
                  f'(当前 cookie 数: {len(found)})   ', end='', flush=True)
            await asyncio.sleep(POLL_INTERVAL)
    finally:
        print()
        await context.close()

    return jar


def import_to_pool(label: str, cookies: dict, proxy_url: str,
                   daily_quota: int | None = None) -> str:
    """导入 Cookie 池并绑定代理, 返回 account_id."""
    missing = [k for k in REQUIRED_COOKIES if k not in cookies]
    if missing:
        raise ValueError(f"缺少必需 cookie: {missing}, 拒绝导入")

    cookie_pool = CookiePoolManager()   # 走 config.REDIS_* 默认连接
    ip_pool = IPPoolManager()

    # 代理: 已存在同 URL 则复用, 否则注册 (内部做健康检查)
    proxy_id = ''
    if proxy_url:
        existing = None
        for node in ip_pool.proxies.values():
            if node.proxy_url == proxy_url:
                existing = node
                break
        if existing is not None:
            proxy_id = existing.proxy_id
            logger.info(f"复用已有代理 {proxy_id} ({proxy_url})")
        else:
            try:
                proxy_id = ip_pool.add_proxy(proxy_url, region='US')
            except Exception as e:
                logger.warning(f"代理注册失败 ({e}), 账号将不绑定代理继续导入")

    quota = daily_quota or config.DAILY_QUOTA_PRODUCTS
    account_id = cookie_pool.add_account(
        username=label,
        cookies=cookies,
        proxy_id=proxy_id,
        daily_quota=quota,
    )

    # 备份 session 文件到 data/sessions/{label}.json (防 data/session.json 被覆盖)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    backup = SESSIONS_DIR / f'{label}.json'
    backup.write_text(json.dumps({
        'cookies': cookies,
        'label': label,
        'account_id': account_id,
        'proxy_id': proxy_id,
        'imported_at': datetime.now().isoformat(),
        'source': 'scripts/pool_add_account.py',
    }, indent=2, ensure_ascii=False), encoding='utf-8')

    print(f'[OK] 账号已入池: label={label} -> account_id={account_id}')
    print(f'[OK] 绑定代理: {proxy_id or "(无)"} | 日配额: {quota}')
    print(f'[OK] session 备份: {backup}')
    stats = cookie_pool.get_pool_stats()
    print(f'[OK] 池状态: {stats}')
    return account_id


async def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description='TikTok 浏览器登录并导入 Cookie 池')
    parser.add_argument('--label', required=True,
                        help='账号标识 (如 acc01), 也是池内 username')
    parser.add_argument('--from-file', type=Path, default=None,
                        help='从已有 session JSON 文件导入 (跳过浏览器登录)')
    parser.add_argument('--proxy', default=DEFAULT_PROXY,
                        help=f'绑定代理 (默认 {DEFAULT_PROXY}, 传空串则不绑定)')
    parser.add_argument('--no-proxy', action='store_true',
                        help='不绑定代理')
    parser.add_argument('--daily-quota', type=int, default=None,
                        help='日配额 (默认取 config.DAILY_QUOTA_PRODUCTS)')
    args = parser.parse_args()

    if args.from_file:
        if not args.from_file.exists():
            print(f'[FAIL] 文件不存在: {args.from_file}')
            return 1
        payload = json.loads(args.from_file.read_text(encoding='utf-8'))
        cookies = payload.get('cookies', payload)  # 兼容裸 cookie dict
        print(f'[*] 从文件导入: {args.from_file} ({len(cookies)} 个 cookie)')
    else:
        cookies = await capture_cookies_via_browser(
            '' if args.no_proxy else args.proxy)
        if not cookies:
            print('[FAIL] 超时未捕获登录态 cookie')
            return 1
        print(f'[OK] 登录态已捕获: {len(cookies)} 个 cookie')

    proxy_url = '' if args.no_proxy else args.proxy
    try:
        import_to_pool(args.label, cookies, proxy_url, args.daily_quota)
    except ValueError as e:
        print(f'[FAIL] {e}')
        return 1
    except Exception as e:
        print(f'[FAIL] 导入失败 ({type(e).__name__}): {e}')
        print('[提示] 检查 Redis 是否可用: redis-cli ping')
        return 2
    return 0


if __name__ == '__main__':
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print('\n[!] 用户中断')
        sys.exit(1)
    except Exception as e:
        print(f'\n[FAIL] {type(e).__name__}: {e}')
        sys.exit(2)
