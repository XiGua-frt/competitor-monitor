"""src.common — 公共层正式实现（Agent A）。

公共导出：
    config     — 全局配置单例（src.common.config.Config 实例）
    get_logger — loguru 日志入口（src.common.logger）
    异常体系   — CrawlerException 基类 + 五个子类

此目录下的正式实现以本版本为准（覆盖此前各 Agent 的 fallback 文件）。
"""

from src.common.config import Config, config
from src.common.exceptions import (
    BotBlockedException,
    CrawlerException,
    ParsingException,
    QuotaExceededException,
    RateLimitException,
    SessionExpiredException,
)
from src.common.logger import get_logger, setup_logger

__all__ = [
    "Config",
    "config",
    "get_logger",
    "setup_logger",
    "CrawlerException",
    "BotBlockedException",
    "RateLimitException",
    "SessionExpiredException",
    "ParsingException",
    "QuotaExceededException",
]
