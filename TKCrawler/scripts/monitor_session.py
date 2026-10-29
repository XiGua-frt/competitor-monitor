#!/usr/bin/env python3
"""TikTok 登录态健康监控脚本 (scripts/monitor_session.py).

功能:
    1. 加载 SessionManager, 调用 is_alive() 检查登录态
       (内部对 https://www.tiktok.com/passport/web/account/info/ 发 1 个
       GET 请求, CurlCffiClient 内置 timeout=30, 故单次运行总耗时 <= 30s)
    2. stdout 输出一行结构化纯文本状态 (便于拷贝/人工阅读)
    3. 结果以一行 JSON 追加写入 data/session_health.log
       (字段: ts / alive / cookies / user_id / error)
    4. 退出码: 0 = alive, 1 = dead (便于外部 automation/调度判断)
    5. dead 时 stdout 额外打印 scripts/login_tiktok.py 的重登命令

副作用约束: 除 is_alive() 内部那 1 个真实请求外无任何网络副作用;
不刷新/不重写 session.json, 只追加健康日志。

用法 (项目根目录):
    python scripts/monitor_session.py
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.anti_detection.curl_client import CurlCffiClient
from src.anti_detection.session_manager import SessionManager

HEALTH_LOG = PROJECT_ROOT / 'data' / 'session_health.log'
# login_tiktok.py 文档中给出的完整可复制命令 (Terminal, 项目根目录执行)
RELOGIN_CMD = (
    '/Users/wuwenbo/.workbuddy/binaries/python/envs/default/bin/python '
    'scripts/login_tiktok.py'
)


def check_session(session_manager: SessionManager = None) -> dict:
    """Run one login-state health check and return the status dict.

    Exactly one real HTTP request is issued (inside is_alive()). The
    passport response is captured by temporarily wrapping
    CurlCffiClient.get so user_id / failure reason can be reported
    without a second request (SessionManager.is_alive() itself only
    returns bool and this file must not modify session_manager.py).
    """
    status = {
        'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'alive': False,
        'cookies': 0,
        'user_id': None,
        'error': None,
    }

    sm = session_manager if session_manager is not None else SessionManager()
    if not sm.load_session():
        status['error'] = 'session file missing or unreadable'
        return status
    status['cookies'] = len(sm._cookies)
    if sm.is_expired():
        status['error'] = ('session expired locally '
                           '(last_refresh older than 24h)')
        return status

    # Wrap CurlCffiClient.get to capture the single passport response
    # is_alive() makes. Restored unconditionally in `finally`.
    captured = {}
    original_get = CurlCffiClient.get

    def capturing_get(client, url, **kwargs):
        response = original_get(client, url, **kwargs)
        captured['response'] = response
        return response

    CurlCffiClient.get = capturing_get
    try:
        status['alive'] = sm.is_alive()
    except Exception as exc:  # defensive; is_alive() swallows internally
        status['alive'] = False
        status['error'] = f'{type(exc).__name__}: {exc}'
        return status
    finally:
        CurlCffiClient.get = original_get

    response = captured.get('response')
    if response is None:
        if not status['alive']:
            # request raised inside is_alive() (network error / WAF block)
            status['error'] = ('request failed (no response captured; '
                               'see log for detail)')
        return status

    try:
        data = response.json()
    except Exception:
        data = {}
    user_id = (data.get('data') or {}).get('user_id')
    if user_id:
        status['user_id'] = str(user_id)
    if not status['alive']:
        message = data.get('message') if isinstance(data, dict) else None
        status['error'] = (f'passport account/info http '
                           f'{response.status_code}: '
                           f'{message or "not logged in"}')
    return status


def format_status(status: dict) -> str:
    """One plain-text status line for stdout."""
    parts = [
        f"ts={status['ts']}",
        f"cookies={status['cookies']}",
        f"is_alive={status['alive']}",
        f"user_id={status['user_id'] if status['user_id'] else '-'}",
    ]
    if status['error']:
        parts.append(f"error={status['error']}")
    return ' | '.join(parts)


def append_health_log(status: dict) -> None:
    """Append one JSON line to data/session_health.log."""
    HEALTH_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(HEALTH_LOG, 'a', encoding='utf-8') as f:
        f.write(json.dumps(status, ensure_ascii=False) + '\n')


def main() -> int:
    status = check_session()
    print(format_status(status))
    append_health_log(status)

    if not status['alive']:
        print('\n[!] 登录态已失效, 请在项目根目录的 Terminal 执行以下命令重新登录:')
        print(f'    {RELOGIN_CMD}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
