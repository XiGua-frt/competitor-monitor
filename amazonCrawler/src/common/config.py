"""配置管理：环境变量 + .env 文件，风格沿用 TKCrawler 的 Config 类。

命名约定：
    优先读 AMZ_ 前缀变量（如 AMZ_MARKETPLACE），回退到通用名（如 MARKETPLACE），
    最后取内置默认值 — 便于与其他爬虫项目共用一个 .env。
数据库连接优先级（在 connection.py 中体现）：
    环境变量 DATABASE_URL  >  POSTGRES_* 拼接  >  DB_PATH (备用 SQLite)
"""

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# 从项目根 / 当前目录加载 .env（不存在则静默跳过）
load_dotenv()


def _env(name: str, default: str = "") -> str:
    """AMZ_ 前缀优先，回退通用名，再回退默认值。"""
    return os.getenv(f"AMZ_{name}", os.getenv(name, default))


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, str(default)).strip().lower()
    if not raw or raw.startswith("#"):  # 行内注释残留视为未配置
        return default
    return raw in ("1", "true", "yes", "on")


class Config:
    """集中式配置（类属性风格，与 TKCrawler 一致）。"""

    # ---- 基础 ----
    MARKETPLACE: str = _env("MARKETPLACE", "www.amazon.com")
    TIMEZONE: str = _env("TIMEZONE", "America/New_York")

    # ---- 数据库 (PostgreSQL 生产 / SQLite 测试) ----
    POSTGRES_HOST: str = _env("POSTGRES_HOST", "localhost")
    POSTGRES_PORT: int = _env_int("POSTGRES_PORT", 5432)
    POSTGRES_DB: str = _env("POSTGRES_DB", "amazon_crawler")
    POSTGRES_USER: str = _env("POSTGRES_USER", "crawler")
    POSTGRES_PASSWORD: str = _env("POSTGRES_PASSWORD", "secret")
    # 备用 SQLite 路径（无 PG 时本地跑通链路用）
    DB_PATH: str = _env("DB_PATH", "data/amazon_crawler.sqlite3")

    @property
    def DATABASE_URL(self) -> str:
        """由 POSTGRES_* 拼接 PostgreSQL 连接串（环境变量 DATABASE_URL 优先级更高，见 connection.py）。"""
        return (
            f"postgresql+psycopg2://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    # ---- Redis（限流 / 日配额）----
    REDIS_HOST: str = _env("REDIS_HOST", "localhost")
    REDIS_PORT: int = _env_int("REDIS_PORT", 6379)
    REDIS_PASSWORD: Optional[str] = os.getenv("AMZ_REDIS_PASSWORD") or os.getenv("REDIS_PASSWORD")
    REDIS_DB: int = _env_int("REDIS_DB", 0)

    # ---- 代理 ----
    PROXY_ENABLED: bool = _env_bool("PROXY_ENABLED", False)
    PROXY_HOST: str = _env("PROXY_HOST", "127.0.0.1")
    PROXY_PORT: int = _env_int("PROXY_PORT", 7892)
    PROXY_TYPE: str = _env("PROXY_TYPE", "http")

    # ---- 代理池（Agent B pools 增强；新增键，不影响已有键语义）----
    # 池开关：BaseScraper use_pool 默认读此键（默认 False，保证向后兼容）
    POOL_ENABLED: bool = _env_bool("POOL_ENABLED", False)
    # 代理来源类型：env（AMZ_PROXY_LIST）| file（AMZ_PROXY_FILE）| api（AMZ_PROXY_API_URL）
    PROXY_SOURCE: str = _env("PROXY_SOURCE", "env")
    # env 来源：逗号分隔代理 URL
    PROXY_LIST: str = _env("PROXY_LIST", "")
    # file 来源：每行一个 URL，支持 # 注释
    PROXY_FILE: str = _env("PROXY_FILE", "data/proxies.txt")
    # api 来源：返回 JSON/文本代理列表的接口地址
    PROXY_API_URL: str = _env("PROXY_API_URL", "")
    # 主动探活开关（ProxyHealthChecker 后台线程）
    HEALTH_CHECK_ENABLED: bool = _env_bool("HEALTH_CHECK_ENABLED", False)
    # 探活周期秒数
    HEALTH_CHECK_INTERVAL: int = _env_int("HEALTH_CHECK_INTERVAL", 600)

    @property
    def PROXY_URL(self) -> Optional[str]:
        """代理开启时返回 proxy URL，否则 None。"""
        if not self.PROXY_ENABLED:
            return None
        return f"{self.PROXY_TYPE}://{self.PROXY_HOST}:{self.PROXY_PORT}"

    # ---- 请求节奏（Amazon 需要比 TK 更保守的间隔）----
    REQUEST_MIN_INTERVAL: int = _env_int("REQUEST_MIN_INTERVAL", 8)
    REQUEST_MAX_INTERVAL: int = _env_int("REQUEST_MAX_INTERVAL", 15)

    # ---- 日配额 ----
    DAILY_QUOTA_ASINS: int = _env_int("DAILY_QUOTA_ASINS", 200)
    DAILY_QUOTA_SEARCH: int = _env_int("DAILY_QUOTA_SEARCH", 50)

    # ---- 反爬软冷却 ----
    SOFT_COOLDOWN_SECONDS: int = _env_int("SOFT_COOLDOWN_SECONDS", 3600)

    # ---- 日志 ----
    LOG_LEVEL: str = _env("LOG_LEVEL", "INFO").upper()
    LOG_FILE: Path = Path(_env("LOG_FILE", "logs/amazon_crawler.log"))

    # ---- 导出 ----
    EXPORT_DIR: Path = Path(_env("EXPORT_DIR", "exports"))

    def __init__(self):
        """确保日志/导出目录存在。"""
        try:
            self.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            self.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            # 只读环境（如 CI）下目录创建失败不阻塞 import
            pass


# 全局配置单例
config = Config()
