"""Data models for pool management."""

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional
from datetime import datetime
from enum import Enum


class AccountStatus(Enum):
    """Account status enum."""
    ACTIVE = 'active'           # 正常活跃
    COOLING = 'cooling'         # 冷却中
    SUSPENDED = 'suspended'     # 已暂停
    FAILED = 'failed'           # 已失效


class ProxyStatus(Enum):
    """Proxy status enum."""
    ACTIVE = 'active'           # 可用
    FAILED = 'failed'           # 失效
    TESTING = 'testing'         # 测试中


@dataclass
class AccountSession:
    """Account session with cookies and metadata."""
    
    # 基础信息
    account_id: str
    username: str
    cookies: Dict[str, str]
    proxy_id: str
    fingerprint_id: str = ''
    
    # 状态管理
    status: str = AccountStatus.ACTIVE.value
    health_score: float = 1.0
    last_request_time: Optional[datetime] = None
    daily_request_count: int = 0
    daily_quota: int = 200
    
    # 风控指标
    waf_block_count: int = 0
    rate_limit_count: int = 0
    last_waf_time: Optional[datetime] = None
    cooling_until: Optional[datetime] = None
    
    # 业务指标
    success_rate: float = 1.0
    avg_response_time: float = 0.0
    total_requests: int = 0
    success_requests: int = 0
    
    # 账号信息
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> dict:
        """Convert to dictionary for Redis storage."""
        data = asdict(self)
        # Convert datetime to ISO string
        for key, value in data.items():
            if isinstance(value, datetime):
                data[key] = value.isoformat() if value else None
        # Redis HSET 不接受 None（redis-py 抛 DataError），统一落为空字符串；
        # from_dict 对空字符串会跳过转换，可安全往返
        for key, value in data.items():
            if value is None:
                data[key] = ''
        return data
    
    @classmethod
    def from_dict(cls, data: dict) -> 'AccountSession':
        """Create from dictionary."""
        # Convert ISO string to datetime
        for key in ['last_request_time', 'last_waf_time', 'cooling_until',
                    'created_at', 'updated_at']:
            if key in data and data[key]:
                data[key] = datetime.fromisoformat(data[key])
            elif key in data and data[key] == '':
                data[key] = None
        return cls(**data)
    
    def update_success_rate(self):
        """Update success rate based on total and success requests."""
        if self.total_requests > 0:
            self.success_rate = self.success_requests / self.total_requests
    
    def is_available(self) -> bool:
        """Check if account is available for use."""
        if self.status != AccountStatus.ACTIVE.value:
            return False
        
        if self.daily_request_count >= self.daily_quota:
            return False
        
        if self.cooling_until and datetime.now() < self.cooling_until:
            return False
        
        return True
    
    def get_weight(self) -> float:
        """Calculate account weight for weighted scheduling."""
        # 权重 = 健康分数 * 剩余配额比例
        quota_remaining = 1 - (self.daily_request_count / self.daily_quota)
        return self.health_score * quota_remaining


@dataclass
class ProxyNode:
    """Proxy node with metadata."""
    
    # 基础信息
    proxy_id: str
    proxy_url: str              # http://user:pass@host:port
    proxy_type: str = 'http'    # http/socks5
    region: str = 'US'          # US/EU/APAC
    
    # 状态管理
    status: str = ProxyStatus.ACTIVE.value
    health_score: float = 1.0
    last_check_time: Optional[datetime] = None
    
    # 绑定关系
    bound_accounts: List[str] = field(default_factory=list)
    max_accounts: int = 3       # 单代理最大账号数
    
    # 性能指标
    avg_latency: float = 0.0    # 平均延迟(ms)
    success_rate: float = 1.0
    total_checks: int = 0
    success_checks: int = 0
    
    # 带宽限制
    bandwidth_limit: int = 0    # MB/day, 0=无限制
    daily_usage: int = 0        # MB
    
    # 其他
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> dict:
        """Convert to dictionary for Redis storage."""
        data = asdict(self)
        for key, value in data.items():
            if isinstance(value, datetime):
                data[key] = value.isoformat() if value else None
        # Redis HSET 不接受 None（redis-py 抛 DataError），统一落为空字符串
        for key, value in data.items():
            if value is None:
                data[key] = ''
        return data

    @classmethod
    def from_dict(cls, data: dict) -> 'ProxyNode':
        """Create from dictionary."""
        for key in ['last_check_time', 'created_at', 'updated_at']:
            if key in data and data[key]:
                data[key] = datetime.fromisoformat(data[key])
            elif key in data and data[key] == '':
                data[key] = None
        return cls(**data)
    
    def is_available(self) -> bool:
        """Check if proxy is available."""
        if self.status != ProxyStatus.ACTIVE.value:
            return False
        
        if len(self.bound_accounts) >= self.max_accounts:
            return False
        
        if self.bandwidth_limit > 0 and self.daily_usage >= self.bandwidth_limit:
            return False
        
        return True
    
    def update_success_rate(self):
        """Update success rate."""
        if self.total_checks > 0:
            self.success_rate = self.success_checks / self.total_checks


@dataclass
class BrowserFingerprint:
    """Browser fingerprint configuration."""
    
    fingerprint_id: str
    
    # 基础参数
    user_agent: str
    viewport: Dict[str, int]    # {'width': 1920, 'height': 1080}
    timezone: str = 'America/Los_Angeles'
    locale: str = 'en-US'
    
    # WebGL指纹
    webgl_vendor: str = 'Google Inc. (Apple)'
    webgl_renderer: str = 'ANGLE (Apple, Apple M1 Pro, OpenGL 4.1)'
    
    # Canvas指纹
    canvas_noise: str = ''      # base64 encoded
    
    # 其他
    screen_resolution: str = '1920x1080'
    cpu_cores: int = 8
    device_memory: int = 16
    hardware_concurrency: int = 8
    platform: str = 'MacIntel'
    
    created_at: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        data = asdict(self)
        if isinstance(data['created_at'], datetime):
            data['created_at'] = data['created_at'].isoformat()
        return data
    
    @classmethod
    def from_dict(cls, data: dict) -> 'BrowserFingerprint':
        """Create from dictionary."""
        if 'created_at' in data and data['created_at']:
            data['created_at'] = datetime.fromisoformat(data['created_at'])
        return cls(**data)


@dataclass
class Task:
    """Task for scheduling."""
    
    task_id: str
    task_type: str              # scrape_product/scrape_video/scrape_creator
    target: str                 # product_id/video_id/creator_id
    priority: int = 1           # 1-10, higher is more important
    retry_count: int = 0
    max_retries: int = 3
    
    created_at: datetime = field(default_factory=datetime.now)
    assigned_account: Optional[str] = None
    
    def should_retry(self) -> bool:
        """Check if task should be retried."""
        return self.retry_count < self.max_retries
