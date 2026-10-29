"""Configuration management using environment variables and .env files."""

import os
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


class Config:
    """Centralized configuration management."""
    
    # TikTok Account
    TIKTOK_USERNAME: str = os.getenv('TIKTOK_USERNAME', '')
    TIKTOK_PASSWORD: str = os.getenv('TIKTOK_PASSWORD', '')
    
    # Database
    POSTGRES_HOST: str = os.getenv('POSTGRES_HOST', 'localhost')
    POSTGRES_PORT: int = int(os.getenv('POSTGRES_PORT', '5432'))
    POSTGRES_DB: str = os.getenv('POSTGRES_DB', 'tiktok_crawler')
    POSTGRES_USER: str = os.getenv('POSTGRES_USER', 'crawler')
    POSTGRES_PASSWORD: str = os.getenv('POSTGRES_PASSWORD', 'secret')
    
    @property
    def DATABASE_URL(self) -> str:
        """Get PostgreSQL connection URL."""
        return (
            f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )
    
    # Redis
    REDIS_HOST: str = os.getenv('REDIS_HOST', 'localhost')
    REDIS_PORT: int = int(os.getenv('REDIS_PORT', '6379'))
    REDIS_PASSWORD: Optional[str] = os.getenv('REDIS_PASSWORD')
    REDIS_DB: int = int(os.getenv('REDIS_DB', '0'))
    
    # Proxy
    PROXY_ENABLED: bool = os.getenv('PROXY_ENABLED', 'true').lower() == 'true'
    PROXY_HOST: str = os.getenv('PROXY_HOST', '127.0.0.1')
    PROXY_PORT: int = int(os.getenv('PROXY_PORT', '7892'))
    PROXY_TYPE: str = os.getenv('PROXY_TYPE', 'http')
    
    @property
    def PROXY_URL(self) -> Optional[str]:
        """Get proxy URL if enabled."""
        if not self.PROXY_ENABLED:
            return None
        return f"{self.PROXY_TYPE}://{self.PROXY_HOST}:{self.PROXY_PORT}"
    
    # Rate Limiting
    REQUEST_MIN_INTERVAL: int = int(os.getenv('REQUEST_MIN_INTERVAL', '5'))
    REQUEST_MAX_INTERVAL: int = int(os.getenv('REQUEST_MAX_INTERVAL', '8'))
    
    # Daily Quotas
    DAILY_QUOTA_PRODUCTS: int = int(os.getenv('DAILY_QUOTA_PRODUCTS', '200'))
    DAILY_QUOTA_VIDEOS: int = int(os.getenv('DAILY_QUOTA_VIDEOS', '500'))
    DAILY_QUOTA_CREATORS: int = int(os.getenv('DAILY_QUOTA_CREATORS', '100'))
    DAILY_QUOTA_HASHTAGS: int = int(os.getenv('DAILY_QUOTA_HASHTAGS', '50'))
    
    # Anti-Detection
    PRIMARY_CHANNEL: str = os.getenv('PRIMARY_CHANNEL', 'curl_cffi')
    CURL_CFFI_IMPERSONATE: str = os.getenv('CURL_CFFI_IMPERSONATE', 'chrome131')
    BROWSER_HEADLESS: bool = os.getenv('BROWSER_HEADLESS', 'true').lower() == 'true'
    # 防御: 行内注释残留(如 "# 留空使用...")或纯空白一律视为未配置
    _raw_exec_path = (os.getenv('BROWSER_EXECUTABLE_PATH') or '').strip()
    BROWSER_EXECUTABLE_PATH: Optional[str] = _raw_exec_path if _raw_exec_path and not _raw_exec_path.startswith('#') else None
    
    # Pool Integration (Cookie 池接入采集链路, Phase 3/4; 全部默认关闭)
    POOL_ENABLED: bool = os.getenv('POOL_ENABLED', 'false').lower() == 'true'
    POOL_SHADOW_MODE: bool = os.getenv('POOL_SHADOW_MODE', 'false').lower() == 'true'
    POOL_LEASE_TTL_SECONDS: int = int(os.getenv('POOL_LEASE_TTL_SECONDS', '600'))
    POOL_STRATEGY: str = os.getenv('POOL_STRATEGY', 'weighted')
    # fail_closed: 池开启但不可用时任务失败(退出码 2), 不静默降级;
    # fallback_single: 池异常时记 WARNING 后显式回退现有单 session 路径
    POOL_FAIL_POLICY: str = os.getenv('POOL_FAIL_POLICY', 'fail_closed')
    # IP 池注入: 开启后租约账号按绑定关系从 IP 池取代理注入 client.session.proxies,
    # 覆盖 config.PROXY_URL; 池中无可用代理时记 WARNING 并保留原 PROXY_URL 配置
    POOL_IP_ENABLED: bool = os.getenv('POOL_IP_ENABLED', 'false').lower() == 'true'

    # Scheduler
    SNAPSHOT_INTERVAL_DAYS: int = int(os.getenv('SNAPSHOT_INTERVAL_DAYS', '2'))
    MAX_RETRY_ATTEMPTS: int = int(os.getenv('MAX_RETRY_ATTEMPTS', '3'))
    RETRY_BACKOFF_BASE: int = int(os.getenv('RETRY_BACKOFF_BASE', '60'))
    
    # External Data Sources
    ALIBABA_APP_KEY: Optional[str] = os.getenv('ALIBABA_APP_KEY')
    ALIBABA_APP_SECRET: Optional[str] = os.getenv('ALIBABA_APP_SECRET')
    KALODATA_API_KEY: Optional[str] = os.getenv('KALODATA_API_KEY')
    ECHOTIK_API_KEY: Optional[str] = os.getenv('ECHOTIK_API_KEY')
    
    # Logging
    LOG_LEVEL: str = os.getenv('LOG_LEVEL', 'INFO')
    LOG_FILE: Path = Path(os.getenv('LOG_FILE', 'logs/crawler.log'))
    
    # Monitoring
    METRICS_PORT: int = int(os.getenv('METRICS_PORT', '9090'))
    SENTRY_DSN: Optional[str] = os.getenv('SENTRY_DSN')
    
    # Export
    EXPORT_DIR: Path = Path(os.getenv('EXPORT_DIR', 'exports'))
    
    # Other
    TIMEZONE: str = os.getenv('TIMEZONE', 'America/Los_Angeles')
    DEBUG: bool = os.getenv('DEBUG', 'false').lower() == 'true'
    
    def __init__(self):
        """Ensure required directories exist."""
        self.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.EXPORT_DIR.mkdir(parents=True, exist_ok=True)


# Global config instance
config = Config()
