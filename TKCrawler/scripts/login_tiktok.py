#!/usr/bin/env python3
"""TikTok 半自动登录脚本 (Terminal 手动运行版).

为什么需要从 Terminal 运行而不是在 WorkBuddy 里跑:
    WorkBuddy App (Electron + Hardened Runtime) 的进程树内, Firefox 子进程
    自身的 macOS seatbelt 沙箱初始化会失败 (sandbox_init: Operation not
    permitted), 导致内容进程无法创建, playwright 的 new_page 永久挂死。
    从 Terminal 启动则不受影响。

用法 (项目根目录):
    /Users/wuwenbo/.workbuddy/binaries/python/envs/default/bin/python scripts/login_tiktok.py

流程:
    1. 弹出 camoufox 有界面浏览器 (回退 playwright firefox), 打开 TikTok 登录页
    2. 人工完成登录 (含验证码/2FA)
    3. 脚本每 2s 轮询 sessionid cookie, 成功后自动保存到 data/session.json
       (格式与 src/anti_detection/session_manager.py 的 SessionManager 完全兼容)

退出码: 0 成功 / 1 超时或失败 / 2 环境错误
"""
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

SESSION_FILE = PROJECT_ROOT / 'data' / 'session.json'
LOGIN_URL = 'https://www.tiktok.com/login/phone-or-email/email'
POLL_INTERVAL = 2          # 秒
TIMEOUT = 480              # 秒, 人工登录给足时间
PROXY = 'http://127.0.0.1:7892'  # 本地干净出口; 不需要代理则设为空串
LOGIN_COOKIES = ('sessionid', 'sessionid_ss')  # 登录态标志 cookie


def save_session(cookies: dict) -> None:
    """按 SessionManager 的格式落盘。"""
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'cookies': cookies,
        'last_refresh': datetime.now().isoformat(),
        'source': 'scripts/login_tiktok.py',
    }
    SESSION_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                            encoding='utf-8')


async def poll_login(context) -> dict | None:
    """轮询 context cookie, 出现登录态 cookie 即返回 cookie dict。"""
    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        try:
            raw = await context.cookies('https://www.tiktok.com')
        except Exception as e:  # 浏览器关闭等
            print(f'[!] cookie 轮询异常: {e}')
            return None
        jar = {c['name']: c['value'] for c in raw}
        if any(name in jar for name in LOGIN_COOKIES):
            return jar
        remaining = int(deadline - time.time())
        print(f'\r[.] 等待登录完成... 剩余 {remaining}s '
              f'(当前 cookie 数: {len(jar)})   ', end='', flush=True)
        await asyncio.sleep(POLL_INTERVAL)
    return None


async def main() -> int:
    proxy = {'server': PROXY} if PROXY else None

    # 优先 camoufox (反检测指纹强), 内核缺失则回退 playwright firefox
    try:
        from camoufox.async_api import AsyncCamoufox
        launcher = 'camoufox'
    except ImportError:
        AsyncCamoufox = None
        launcher = 'playwright-firefox'

    print(f'[*] 启动浏览器: {launcher} (有界面模式)')
    print(f'[*] 登录页: {LOGIN_URL}')
    print('[*] 请在弹出的浏览器窗口中完成登录 (含验证码)')
    print(f'[*] 超时 {TIMEOUT}s, 登录成功后自动捕获 Cookie\n')

    if AsyncCamoufox is not None:
        cm = AsyncCamoufox(headless=False, proxy=proxy,
                           i_know_what_im_doing=True)
    else:
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        cm = pw.firefox.launch(headless=False, proxy=proxy)

    browser_or_ctx = await cm.__aenter__()
    # camoufox 返回 Browser; firefox.launch 同样返回 Browser
    context = await browser_or_ctx.new_context()
    page = await context.new_page()
    await page.goto(LOGIN_URL, wait_until='domcontentloaded')
    print('[*] 页面已打开, 开始轮询登录态...')

    jar = None
    try:
        jar = await poll_login(context)
    finally:
        print()
        if jar:
            save_session(jar)
            print(f'[OK] 登录态已捕获: {len(jar)} 个 cookie')
            print(f'[OK] 已保存到 {SESSION_FILE}')
            names = sorted(jar.keys())
            print(f'[OK] cookie 列表: {names}')
        else:
            print('[FAIL] 超时未捕获登录态 cookie')
        await context.close()

    return 0 if jar else 1


if __name__ == '__main__':
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print('\n[!] 用户中断')
        sys.exit(1)
    except Exception as e:
        print(f'\n[FAIL] {type(e).__name__}: {e}')
        sys.exit(2)
