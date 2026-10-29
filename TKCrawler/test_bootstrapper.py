"""Test script for AutoLoginBootstrapper."""

import asyncio
import json
import redis
from pathlib import Path
from src.pools.bootstrapper import AutoLoginBootstrapper
from src.pools.cookie_pool import CookiePoolManager
from src.pools.ip_pool import IPPoolManager
from src.common.logger import get_logger

logger = get_logger(__name__)


async def test_batch_login():
    """测试批量登录功能."""
    
    # 初始化 Redis（使用测试数据库）
    redis_client = redis.Redis(
        host='localhost',
        port=6379,
        db=15,  # 测试数据库
        decode_responses=True
    )
    
    # 清空测试数据库
    redis_client.flushdb()
    logger.info("Test database cleared")
    
    # 初始化池管理器
    cookie_pool = CookiePoolManager(redis_client)
    ip_pool = IPPoolManager(redis_client)
    
    # 添加测试代理（可选）
    try:
        proxy_id = ip_pool.add_proxy(
            proxy_url='http://user:pass@proxy.example.com:8080',
            region='US'
        )
        logger.info(f"Test proxy added: {proxy_id}")
    except Exception as e:
        logger.warning(f"Failed to add test proxy: {e}")
    
    # 创建 Bootstrapper
    bootstrapper = AutoLoginBootstrapper(
        cookie_pool=cookie_pool,
        ip_pool=ip_pool,
        headless=False,  # 显示浏览器，方便人工验证
        save_cookies_dir=Path('./exports/cookies/test')
    )
    
    # 测试账号（请替换为真实账号）
    test_accounts = [
        {
            "phone": "13800138000",
            "password": "test_password_1"
        },
        {
            "phone": "13800138001", 
            "password": "test_password_2"
        },
        {
            "phone": "13800138002",
            "password": "test_password_3"
        }
    ]
    
    logger.info(f"Starting batch login for {len(test_accounts)} accounts")
    logger.warning("⚠️  Please complete CAPTCHA manually when browser window appears")
    
    # 执行批量登录
    results = await bootstrapper.batch_login(
        accounts=test_accounts,
        max_concurrent=1  # 逐个登录
    )
    
    # 统计结果
    success_count = len(results)
    total_count = len(test_accounts)
    
    print("\n" + "="*60)
    print(f"📊 批量登录测试结果")
    print("="*60)
    print(f"✅ 成功: {success_count}/{total_count}")
    print(f"❌ 失败: {total_count - success_count}/{total_count}")
    print()
    
    if results:
        print("成功账号列表:")
        for phone, account_id in results.items():
            print(f"  📱 {phone} -> {account_id}")
            
            # 验证登录状态
            is_valid = await bootstrapper.verify_login_status(account_id)
            print(f"     验证状态: {'✅ 有效' if is_valid else '❌ 无效'}")
            
            # 查看账号详情
            account = cookie_pool._load_account(account_id)
            if account:
                print(f"     Cookies 数量: {len(account.cookies)}")
                print(f"     绑定代理: {account.proxy_id or '无'}")
                print(f"     健康分数: {account.health_score:.2f}")
            print()
    else:
        print("❌ 所有账号登录失败")
    
    print("="*60)
    
    # 检查 Cookie 备份文件
    backup_dir = Path('./exports/cookies/test')
    if backup_dir.exists():
        backup_files = list(backup_dir.glob('*.json'))
        print(f"\n💾 Cookie 备份文件: {len(backup_files)}")
        for f in backup_files:
            print(f"  - {f.name}")
    
    # 清理
    redis_client.close()
    logger.info("Test completed")


async def test_manual_import():
    """测试手动导入 Cookies."""
    
    redis_client = redis.Redis(host='localhost', port=6379, db=15, decode_responses=True)
    redis_client.flushdb()
    
    cookie_pool = CookiePoolManager(redis_client)
    ip_pool = IPPoolManager(redis_client)
    
    bootstrapper = AutoLoginBootstrapper(
        cookie_pool=cookie_pool,
        ip_pool=ip_pool
    )
    
    # 模拟已提取的 Cookies
    mock_cookies = {
        'sessionid': 'mock_session_' + 'x'*50,
        'sessionid_ss': 'mock_session_ss_' + 'y'*40,
        'ttwid': '1%7C' + 'z'*127,
        '__ac_nonce': 'mock_nonce_' + 'a'*30,
        '__ac_signature': 'mock_sig_' + 'b'*40,
        'msToken': 'mock_token_' + 'c'*168,
        's_v_web_id': 'verify_' + 'd'*20,
        'passport_csrf_token': 'csrf_' + 'e'*32,
        'sid_guard': 'guard_' + 'f'*40,
        'uid_tt': '12345678',
        # ... 其他字段
    }
    
    print("\n" + "="*60)
    print("📦 测试手动导入 Cookies")
    print("="*60)
    
    # 导入
    account_id = await bootstrapper.import_to_pool(
        phone='13900000000',
        cookies=mock_cookies,
        proxy_id=''
    )
    
    print(f"✅ 导入成功: {account_id}")
    
    # 验证
    account = cookie_pool._load_account(account_id)
    print(f"📋 账号详情:")
    print(f"  Username: {account.username}")
    print(f"  Cookies 数量: {len(account.cookies)}")
    print(f"  状态: {account.status}")
    print(f"  健康分数: {account.health_score}")
    print(f"  日配额: {account.daily_quota}")
    print("="*60)
    
    redis_client.close()


async def test_cookies_validation():
    """测试 Cookies 字段验证."""
    
    print("\n" + "="*60)
    print("🔍 Cookies 字段验证测试")
    print("="*60)
    
    # 完整的 27 个字段
    required_fields = AutoLoginBootstrapper.REQUIRED_COOKIES
    
    print(f"📋 必需字段清单 ({len(required_fields)} 个):")
    
    critical = required_fields[:5]
    important = required_fields[5:10]
    auxiliary = required_fields[10:]
    
    print("\n🔴 核心字段 (Critical):")
    for i, field in enumerate(critical, 1):
        print(f"  {i}. {field}")
    
    print("\n🟡 重要字段 (Important):")
    for i, field in enumerate(important, 1):
        print(f"  {i}. {field}")
    
    print("\n🟢 辅助字段 (Auxiliary):")
    for i, field in enumerate(auxiliary, 1):
        print(f"  {i}. {field}")
    
    print("\n" + "="*60)
    
    # 模拟缺失字段检测
    mock_cookies = {
        'sessionid': 'test',
        'ttwid': 'test',
        # 故意缺少其他字段
    }
    
    missing = [k for k in required_fields if k not in mock_cookies]
    print(f"\n⚠️  示例 Cookies 缺失字段: {len(missing)}/{len(required_fields)}")
    print(f"缺失列表: {', '.join(missing[:10])}...")
    print("="*60)


if __name__ == '__main__':
    import sys
    
    print("""
╔═══════════════════════════════════════════════════════════╗
║        AutoLoginBootstrapper 测试套件                      ║
╚═══════════════════════════════════════════════════════════╝

测试项目:
  1. 批量登录测试 (batch_login)
  2. 手动导入测试 (manual_import)  
  3. Cookies 字段验证 (validation)

使用方法:
  python test_bootstrapper.py [test_name]
  
示例:
  python test_bootstrapper.py batch_login
  python test_bootstrapper.py manual_import
  python test_bootstrapper.py validation
    """)
    
    if len(sys.argv) < 2:
        print("❌ 请指定测试项目")
        sys.exit(1)
    
    test_name = sys.argv[1]
    
    if test_name == 'batch_login':
        asyncio.run(test_batch_login())
    elif test_name == 'manual_import':
        asyncio.run(test_manual_import())
    elif test_name == 'validation':
        asyncio.run(test_cookies_validation())
    else:
        print(f"❌ 未知测试项目: {test_name}")
        sys.exit(1)
