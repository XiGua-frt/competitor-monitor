# TikTok 跨境选品数据采集系统 (TKCrawler)

> 构建数据驱动的TikTok选品决策系统

## 📋 项目概述

TKCrawler 是一个面向TikTok跨境电商的选品数据采集与分析系统,旨在将"什么好卖、谁在推、推得怎么样"转化为可量化、可追踪的结构化数据,替代人工刷广场拍脑袋选品。

### 核心能力
- **商品数据采集**: 类目榜单、商品详情15字段、GMV增速计算
- **视频内容分析**: 关联视频、播放指标、内容效率评估
- **达人数据追踪**: 粉丝数、带货能力、橱窗商品
- **反检测架构**: curl_cffi + camoufox双通道,绕过TikTok风控
- **快照差分引擎**: 周期采集 + 时序差分,识别飙升品

### 数据用途
```
发现(什么在涨) → 初筛(能不能做) → 深筛(做有没有得赚) → 测试(内容行不行) → 放大(要不要压货)
```

## 🚀 快速开始

### 前置要求
- Python 3.9+
- PostgreSQL 13+
- Redis 6+
- Docker & Docker Compose

### 一键部署
```bash
# 克隆仓库
git clone <repo-url>
cd TKCrawler

# 一键安装环境(创建venv、装依赖、生成.env)
bash scripts/setup.sh

# 编辑 .env 填入TikTok账号等配置

# 启动数据层服务
# 注意: crawler/prometheus/grafana 服务所需文件(Dockerfile、main.py、
# docs/monitoring/)尚未交付, 当前阶段只启动数据层
docker-compose up -d postgres redis

# 初始化数据库
python scripts/init_db.py

# 验证环境
pytest tests/ -v
```

> 采集入口 CLI (`main.py`) 尚在开发中, 当前可运行 `pytest` 验证核心模块, 或直接使用 `src/scrapers/shop_scraper.py` 的 `ShopProductScraper` 类。

### 手动安装
```bash
# 安装依赖
pip install -r requirements.txt

# 启动PostgreSQL和Redis(手动或Docker)
docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=secret postgres:13
docker run -d -p 6379:6379 redis:6

# 初始化数据库
python scripts/init_db.py

# 验证
python scripts/init_db.py --check
```

## 📁 项目结构

```
TKCrawler/
├── src/
│   ├── anti_detection/      # 反检测基建
│   │   ├── curl_client.py       # curl_cffi封装
│   │   ├── browser_client.py    # camoufox封装
│   │   ├── session_manager.py   # Cookie/会话管理
│   │   └── rate_limiter.py      # 速率控制
│   ├── database/            # 数据库层
│   │   ├── models.py            # ORM模型
│   │   ├── snapshot_engine.py   # 快照差分引擎
│   │   └── migrations/          # 数据库迁移
│   ├── scheduler/           # 调度系统
│   │   ├── task_scheduler.py    # 周期调度器
│   │   └── task_queue.py        # 优先级队列
│   ├── scrapers/            # 数据采集层
│   │   ├── shop_scraper.py      # Shop商品页
│   │   ├── video_scraper.py     # 视频流
│   │   ├── creator_scraper.py   # 达人主页
│   │   └── hashtag_scraper.py   # 话题/标签
│   ├── external_data/       # 外部数据集成
│   │   ├── alibaba_client.py    # 1688供应链
│   │   ├── trends_client.py     # Google Trends
│   │   └── amazon_client.py     # Amazon BSR
│   ├── metrics/             # 派生指标计算
│   │   └── calculator.py        # 业务指标计算
│   └── common/              # 公共模块
│       ├── config.py            # 配置管理
│       ├── logger.py            # 日志
│       └── exceptions.py        # 自定义异常
├── tests/                   # 测试
├── docs/                    # 文档
│   ├── DEPLOYMENT.md            # 部署指南
│   ├── FIELD_SCHEMA.md          # 字段说明
│   └── monitoring/              # 监控配置
├── scripts/                 # 工具脚本
├── docker-compose.yml       # Docker编排
├── requirements.txt         # Python依赖
└── .env.example             # 环境变量模板
```

## 📊 核心字段说明

### 商品维度(15字段)
| 字段 | 说明 | 用途 |
|------|------|------|
| product_id | 商品唯一ID | 主键、去重、追踪 |
| title | 商品标题 | 品类归类、关键词提取 |
| price_current | 当前售价(USD) | 价格带筛选 |
| sales_30d | 近30天销量 | 需求规模 |
| gmv_30d_est | 近30天GMV估算 | 市场规模排序 |
| gmv_growth_7d | 7天销量增速 | **识别飙升品(核心)** |
| creator_count | 关联带货达人数 | 分发广度 |
| video_count_7d | 近7天关联视频数 | 内容热度 |
| ... | 完整字段见文档 | ... |

详细字段说明: [FIELD_SCHEMA.md](docs/FIELD_SCHEMA.md)

## 🔧 配置说明

### 环境变量
```bash
# TikTok账号(用于获取登录态)
TIKTOK_USERNAME=your_username
TIKTOK_PASSWORD=your_password

# 数据库
POSTGRES_HOST=localhost
POSTGRES_DB=tiktok_crawler

# 速率限制
REQUEST_MIN_INTERVAL=5  # 最小间隔(秒)
REQUEST_MAX_INTERVAL=8  # 最大间隔(秒)
DAILY_QUOTA_PRODUCTS=200  # 每日商品配额
DAILY_QUOTA_VIDEOS=500    # 每日视频配额
```

### 调度配置
编辑 `src/scheduler/config/tasks.yaml`:
```yaml
tasks:
  - name: scrape_shop_products
    type: periodic
    interval: 2d  # 2天一次快照
    priority: 1
    retry:
      max_attempts: 3
      backoff: exponential
```

## 📈 监控与运维

### 监控面板
访问 Grafana: http://localhost:3000 (默认账号: admin/admin)

关键指标:
- 请求成功率
- WAF拦截次数
- 数据质量(空字段率)
- 采集速率

### 日志查看
```bash
# 实时日志
docker-compose logs -f crawler

# 错误日志
tail -f logs/error.log
```

## 🛡️ 风控策略

1. **速率控制**: 5-8秒随机间隔,避免触发WAF
2. **单会话红线**: 不做多账号池化,降低账号风险
3. **软冷却机制**: 遇到429/403立即停止,等待1小时
4. **配额限制**: 单日200商品页 + 500视频(可配置)
5. **双通道切换**: curl_cffi主通道被拦截时自动切换camoufox

## 📝 开发指南

### 添加新的采集器
1. 在 `src/scrapers/` 创建 `xxx_scraper.py`
2. 继承 `BaseScraper` 实现 `scrape()` 方法
3. 在 `src/scheduler/config/tasks.yaml` 添加调度配置

### 运行测试
```bash
# 运行所有测试
pytest

# 运行特定测试
pytest tests/test_shop_scraper.py -v

# 测试覆盖率
pytest --cov=src tests/
```

## 🤝 参考项目

- [Evil0ctal/Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API) - 架构参考
- [lexiforest/curl_cffi](https://github.com/lexiforest/curl_cffi) - TLS指纹模拟
- [daijro/camoufox](https://github.com/daijro/camoufox) - 反检测浏览器

## 📄 相关文档

- [Agent任务卡](AGENT_TASK_CARDS.md) - 多Agent并行开发计划
- [数据采集清单](tk-selection-data-schema.md) - 完整字段定义
- [抓取目的说明](tk-scraping-purpose.md) - 业务逻辑
- [GitHub参考仓库](tk-crawler-github-references.md) - 技术选型

## ⚖️ 合规声明

本项目仅用于合法的商业分析用途:
- 只抓取公开数据
- 使用用户授权的会话Cookie
- 严格遵守速率限制
- 不做池化绕过或批量注册

## 📧 联系方式

有问题或建议? 欢迎提Issue或Pull Request!

## 📜 开源协议

MIT License
