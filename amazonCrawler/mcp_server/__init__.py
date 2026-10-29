"""amazonCrawler MCP Server 包。

公开 FastMCP 实例与依赖注入入口；python -m mcp_server 启动 stdio 服务。
"""

from mcp_server.server import (
    SERVICE_IMPORT_STATUS,
    configure_services,
    main,
    mcp,
    reset_services,
)

__all__ = [
    "mcp",
    "main",
    "configure_services",
    "reset_services",
    "SERVICE_IMPORT_STATUS",
]
