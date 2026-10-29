"""amazonCrawler — Amazon 竞品监控系统（参考 TKCrawler 架构）。

分层: common / anti_detection+pools / scrapers / database(PG 快照差分) /
scheduler / metrics / service / mcp。主键=ASIN+snapshot_date, 周期快照差分出
价格/BSR/评论增速等增量信号。
"""

__version__ = "0.1.0"
