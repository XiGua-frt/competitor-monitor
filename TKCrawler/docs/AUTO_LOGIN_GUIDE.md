# Auto-Login Bootstrapper 使用指南

## 功能概述

`AutoLoginBootstrapper` 提供批量自动登录抖音账号并导入 Cookie 池的能力。

### 核心特性

- ✅ 批量登录多个抖音账号
- ✅ 自动提取 27 个关键 Cookies（`sessionid`, `ttwid`, `__ac_nonce` 等）
- ✅ 支持人工滑块验证（30秒等待窗口）
- ✅ 自动关联代理 IP
- ✅ 导入后自动备份 Cookies 到文件
- ✅ 智能延迟控制（30-60秒随机间隔）

---

## 快速开始

### 1. 安装依赖

```bash
pip install camoufox>=0.4.0
playwright install firefox  # camoufox 依赖
```

### 2. 准备账号文件

创建 `accounts.json`：

```json
[
  {
    "phone": "13800138000",
    "password": "your_password_1"
  },
  {
    "phone": "13800138001",
    "password": "your_password_2"
  },
  {
    "phone": "13800138002",
    "password": "your_password_3"
  }
]
```

### 3. 批量登录

```python
import asyncio
import redis
from pathlib import Path
from src.pools.cookie_pool import CookiePoolManager
from src.pools.ip_pool import IPPoolManager
from src.pools.bootstrapper import AutoLoginBootstrapper

async def main():
    # 初始化 Redis 和池管理器
    redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
    cookie_pool = CookiePoolManager(redis_client)
    ip_pool = IPPoolManager(redis_client)
    
    # 创建 Bootstrapper
    bootstrapper = AutoLoginBootstrapper(
        cookie_pool=cookie_pool,
        ip_pool=ip_pool,
        headless=False,  # False=显示浏览器（方便人工验证滑块）
        save_cookies_dir=Path('./exports/cookies')
    )
    
    # 加载账号列表
    import json
    with open('accounts.json', 'r') as f:
        accounts = json.load(f)
    
    # 批量登录（最大并发1，避免风控）
    results = await bootstrapper.batch_login(accounts, max_concurrent=1)
    
    # 打印结果
    print(f"✅ 成功登录: {len(results)}/{len(accounts)}")
    for phone, account_id in results.items():
        print(f"  - {phone} -> {account_id}")

if __name__ == '__main__':
    asyncio.run(main())
```

---

## 核心 API

### `AutoLoginBootstrapper`

#### 构造函数

```python
bootstrapper = AutoLoginBootstrapper(
    cookie_pool: CookiePoolManager,     # Cookie 池管理器
    ip_pool: IPPoolManager,             # IP 池管理器
    headless: bool = False,             # 是否无头模式（建议 False）
    save_cookies_dir: Path = None       # Cookie 备份目录
)
```

#### `batch_login(accounts, max_concurrent=1)`

批量登录账号。

**参数：**
- `accounts`: `List[Dict]` - 账号列表 `[{"phone": "...", "password": "..."}, ...]`
- `max_concurrent`: `int` - 最大并发数（建议 1）

**返回：**
- `Dict[str, str]` - `{"phone": "account_id", ...}` 成功映射

#### `import_to_pool(phone, cookies, proxy_id='')`

手动导入已有 Cookies。

**参数：**
- `phone`: `str` - 手机号（作为 username）
- `cookies`: `Dict[str, str]` - Cookie 字典
- `proxy_id`: `str` - 绑定的代理 ID（可选）

**返回：**
- `str` - 生成的 `account_id`

---

## 登录流程说明

### 自动化步骤

1. **打开抖音首页** (`https://www.douyin.com/`)
2. **点击登录按钮**
3. **选择手机号登录**
4. **输入手机号**
5. **输入密码**
6. **点击提交**
7. **等待人工验证滑块**（30 秒超时）
8. **提取 27 个 Cookies**
9. **保存备份** → `exports/cookies/{phone}_{timestamp}.json`
10. **导入 Cookie Pool**
11. **延迟 30-60 秒**（随机）

### 人工介入点

⚠️ **滑块验证阶段**

浏览器会暂停 30 秒，等待你手动完成滑块验证：

```
[138XXXX0000] ⚠️  Please complete CAPTCHA manually within 30.0s
```

完成后，程序自动继续。

---

## 提取的 27 个关键 Cookies

| Cookie 名称 | 用途 | 优先级 |
|---|---|---|
| `sessionid` | 会话 ID（最关键） | 🔴 必需 |
| `sessionid_ss` | 会话签名 | 🔴 必需 |
| `ttwid` | TikTok Web ID | 🔴 必需 |
| `__ac_nonce` | 防重放 nonce | 🔴 必需 |
| `__ac_signature` | 签名 | 🔴 必需 |
| `msToken` | 微软令牌 | 🟡 重要 |
| `s_v_web_id` | 设备 ID | 🟡 重要 |
| `passport_csrf_token` | CSRF 令牌 | 🟡 重要 |
| `sid_guard` | 会话守卫 | 🟡 重要 |
| `uid_tt` | 用户 ID | 🟡 重要 |
| ... | 其他 17 个字段 | 🟢 辅助 |

---

## 延迟控制策略

### 登录间隔

- **每个账号登录后**：30-60 秒随机延迟
- **目的**：避免频繁请求触发风控

### 并发控制

- **建议 `max_concurrent=1`**（逐个登录）
- 高并发可能导致 IP 被封

---

## 故障排查

### 1. `camoufox` 未安装

```bash
pip install camoufox>=0.4.0
playwright install firefox
```

### 2. 登录超时（滑块未完成）

- **原因**：30 秒内未完成滑块验证
- **解决**：设置 `headless=False`，手动完成验证

### 3. Cookies 不完整

- **现象**：缺少 `sessionid` 或 `ttwid`
- **原因**：登录未真正成功
- **解决**：检查账号密码，重试

### 4. 代理连接失败

- **现象**：浏览器无法打开页面
- **解决**：检查 `ip_pool` 中的代理是否可用

---

## 高级用法

### 从文件批量导入

```python
from src.pools.bootstrapper import bootstrap_accounts_from_file

results = await bootstrap_accounts_from_file(
    filepath=Path('accounts.json'),
    cookie_pool=cookie_pool,
    ip_pool=ip_pool,
    headless=False
)
```

### 手动导入已有 Cookies

```python
cookies = {
    'sessionid': 'abcd1234...',
    'ttwid': '1%7CyBP...',
    '__ac_nonce': 'xyz...',
    # ... 其他字段
}

account_id = await bootstrapper.import_to_pool(
    phone='13800138000',
    cookies=cookies,
    proxy_id='proxy_abc123'
)
```

### 验证登录状态

```python
is_valid = await bootstrapper.verify_login_status(account_id)
print(f"账号有效性: {is_valid}")
```

---

## Cookie 备份格式

备份文件保存在 `exports/cookies/{phone}_{timestamp}.json`：

```json
{
  "phone": "13800138000",
  "timestamp": "20261008_103045",
  "cookies": {
    "sessionid": "abcd1234...",
    "ttwid": "1%7CyBP...",
    "__ac_nonce": "xyz...",
    "...": "..."
  },
  "cookie_count": 27
}
```

---

## 安全建议

1. **不要在公共网络登录**
2. **使用可信代理**
3. **限制并发数**（`max_concurrent=1`）
4. **定期更换账号**
5. **监控健康分数**（`health_score`）

---

## 测试示例

### 测试 3 个账号批量登录

```bash
# 1. 准备测试账号
cat > test_accounts.json << 'EOF'
[
  {"phone": "13800138000", "password": "test123"},
  {"phone": "13800138001", "password": "test456"},
  {"phone": "13800138002", "password": "test789"}
]
EOF

# 2. 运行测试脚本
python3 << 'SCRIPT'
import asyncio
import redis
from pathlib import Path
from src.pools.bootstrapper import bootstrap_accounts_from_file
from src.pools.cookie_pool import CookiePoolManager
from src.pools.ip_pool import IPPoolManager

async def test():
    redis_client = redis.Redis(host='localhost', port=6379, db=15, decode_responses=True)
    cookie_pool = CookiePoolManager(redis_client)
    ip_pool = IPPoolManager(redis_client)
    
    results = await bootstrap_accounts_from_file(
        filepath=Path('test_accounts.json'),
        cookie_pool=cookie_pool,
        ip_pool=ip_pool,
        headless=False
    )
    
    print(f"\n✅ 测试完成: {len(results)}/3 成功")
    for phone, account_id in results.items():
        print(f"  {phone} -> {account_id}")
    
    redis_client.close()

asyncio.run(test())
SCRIPT
```

---

## 常见问题

### Q1: 为什么建议 `headless=False`？

**A**: 滑块验证需要人工完成，无头模式看不到浏览器界面。

### Q2: 延迟时间可以缩短吗？

**A**: 不建议。30-60 秒是经验值，缩短可能触发风控。

### Q3: 支持验证码自动识别吗？

**A**: 暂不支持。滑块验证依赖人工完成。

### Q4: Cookie 有效期多久？

**A**: 通常 7-30 天，取决于抖音策略和使用频率。

---

## 相关文档

- [Cookie Pool 管理文档](./COOKIE_POOL_GUIDE.md)
- [IP Pool 管理文档](./IP_POOL_GUIDE.md)
- [健康检查系统](./HEALTH_CHECK_GUIDE.md)

---

**最后更新**: 2026-10-08  
**维护者**: TKCrawler Team
