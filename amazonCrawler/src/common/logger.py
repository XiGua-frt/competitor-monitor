"""日志配置（Agent A 正式实现）：loguru，控制台 + 文件轮转。

用法（契约约定）:
    from src.common.logger import get_logger
    logger = get_logger(__name__)

兼容性说明：
    为兼容早期 fallback 代码的 bind(module=...) 风格，get_logger 同时绑定
    extra['name'] 与 extra['module'] 两个键；sink 格式统一读 extra['name']。
"""

import sys

from loguru import logger

from src.common.config import config

_FORMAT_CONSOLE = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{extra[name]}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)
_FORMAT_FILE = (
    "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | "
    "{extra[name]}:{function}:{line} - {message}"
)

_initialized = False


def setup_logger():
    """配置 loguru：stderr 控制台 + 文件（20MB 轮转 / 保留 30 天 / zip 压缩）。

    幂等安全：重复调用只重配 handler，不产生重复输出。
    """
    global _initialized
    # 为未绑定 name 的裸 logger 调用提供 extra 默认值，避免格式化报 KeyError
    logger.configure(extra={"name": "root", "module": "root"})
    logger.remove()
    try:
        logger.add(
            sys.stderr,
            level=config.LOG_LEVEL,
            format=_FORMAT_CONSOLE,
            colorize=True,
        )
        logger.add(
            config.LOG_FILE,
            level=config.LOG_LEVEL,
            format=_FORMAT_FILE,
            rotation="20 MB",
            retention="30 days",
            compression="zip",
            enqueue=False,  # 同步写：短脚本/测试场景进程退出不丢日志
        )
        _initialized = True
    except OSError:
        # 只读文件系统（CI）下仅保留控制台输出
        logger.add(sys.stderr, level=config.LOG_LEVEL, format=_FORMAT_CONSOLE)
        _initialized = True
    return logger


def get_logger(name: str = ""):
    """按模块名获取 logger；未传 name 返回原始 logger。

    同时绑定 extra['name'] / extra['module']，兼容两种读取风格。
    """
    if not _initialized:
        setup_logger()
    if name:
        return logger.bind(name=name, module=name)
    return logger


# import 时初始化（与 TKCrawler 行为一致）
setup_logger()
