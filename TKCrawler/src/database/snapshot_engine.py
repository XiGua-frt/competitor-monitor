"""快照差分引擎 — SnapshotDiffEngine。

对采集层 (Agents D/E/F/G) 暴露的接口契约:
    save_snapshot(data: dict, table: str) -> bool
    calculate_growth(product_id: str, days: int) -> dict
    get_latest_snapshot(product_id: str) -> dict

table 合法取值: 'product_snapshots' / 'video_snapshots' /
'creator_snapshots' / 'hashtag_snapshots'。
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional, Type

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from src.common.logger import get_logger
from src.database import connection as _db_connection
from src.database.models import (
    CreatorSnapshot,
    HashtagSnapshot,
    ProductSnapshot,
    VideoSnapshot,
)

logger = get_logger("database.snapshot_engine")

# table 参数白名单 → ORM 模型映射
TABLE_MODELS: Dict[str, Type] = {
    "product_snapshots": ProductSnapshot,
    "video_snapshots": VideoSnapshot,
    "creator_snapshots": CreatorSnapshot,
    "hashtag_snapshots": HashtagSnapshot,
}

# 各表 upsert 冲突目标 (即主键列)
TABLE_CONFLICT_COLS: Dict[str, list] = {
    "product_snapshots": ["product_id", "snapshot_date"],
    "video_snapshots": ["video_id", "snapshot_date"],
    "creator_snapshots": ["creator_id", "snapshot_date"],
    "hashtag_snapshots": ["tag_name", "snapshot_date"],
}


def _coerce_value(column, value: Any) -> Any:
    """按列类型做基础类型收敛: 字符串日期 → date/datetime。"""
    if value is None:
        return None
    py_type = column.type.python_type
    if py_type is date and isinstance(value, str):
        return date.fromisoformat(value[:10])
    if py_type is datetime and isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def compute_growth_values(
    latest_gmv: Optional[float], past_gmv: Optional[float]
) -> tuple:
    """纯函数: 由两个 GMV 快照值计算 (gmv_growth, growth_rate)。

    NULLIF 语义说明 (与任务卡 SQL 视图一致):
        - 任一输入为 None → 两个输出均为 None (无法计算, 不猜测);
        - past_gmv == 0 → growth_rate 为 None (除零, 对应 SQL NULLIF(prev, 0)),
          gmv_growth 仍返回绝对差值;
        - 正常情况 → gmv_growth = latest - past,
          growth_rate = gmv_growth / past (小数, 0.25 表示 +25%)。
    """
    if latest_gmv is None or past_gmv is None:
        return None, None
    gmv_growth = float(latest_gmv) - float(past_gmv)
    if float(past_gmv) == 0:
        return gmv_growth, None
    return gmv_growth, gmv_growth / float(past_gmv)


class SnapshotDiffEngine:
    """快照写入 + 差分计算引擎。

    Args:
        session_factory: 会话工厂, 默认使用全局 SessionLocal;
            测试时可注入绑定 SQLite 内存库的 sessionmaker。
    """

    def __init__(self, session_factory: Optional[sessionmaker] = None):
        # 延迟取全局 SessionLocal: 模块 import 时不触发 engine 创建(惰性化),
        # 首次实例化 SnapshotDiffEngine 且未注入 session_factory 时才建
        self._session_factory = session_factory or _db_connection.SessionLocal

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def save_snapshot(self, data: dict, table: str) -> bool:
        """按 upsert 语义写入一条快照 (冲突时更新)。

        Args:
            data: 字段字典, 键为表的列名; 未提供的列存 NULL;
                非法的键会被忽略并记录 debug 日志。
            table: 目标表名, 见模块级 TABLE_MODELS 白名单。

        Returns:
            bool: 写入成功 True; 表名非法/参数为空/DB 异常 False。

        冲突语义:
            主键 (实体ID, snapshot_date) 冲突时更新其余字段,
            等价于 PG 的 INSERT ... ON CONFLICT DO UPDATE。
            PG 与 SQLite 走各自方言的 insert.on_conflict_do_update;
            其他方言退化为"查后插/改"的通用实现。
        """
        if not isinstance(data, dict) or not data:
            logger.error("save_snapshot: data 必须为非空 dict, table={}", table)
            return False

        model = TABLE_MODELS.get(table)
        if model is None:
            logger.error("save_snapshot: 非法表名 '{}', 合法值: {}", table, list(TABLE_MODELS))
            return False

        try:
            mapper = sa_inspect(model)
            column_names = set(mapper.column_attrs.keys())
            values = {}
            for key, val in data.items():
                if key in column_names:
                    values[key] = _coerce_value(mapper.columns[key], val)
                else:
                    logger.debug("save_snapshot: 忽略非法字段 '{}' (table={})", key, table)

            missing_pk = [pk for pk in TABLE_CONFLICT_COLS[table] if pk not in values or values[pk] is None]
            if missing_pk:
                logger.error("save_snapshot: 缺少主键字段 {} (table={})", missing_pk, table)
                return False

            with self._session_factory() as session:
                dialect = session.bind.dialect.name
                if dialect == "postgresql":
                    stmt = pg_insert(model).values(**values)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=TABLE_CONFLICT_COLS[table],
                        set_={k: v for k, v in values.items()
                              if k not in TABLE_CONFLICT_COLS[table]},
                    )
                    session.execute(stmt)
                elif dialect == "sqlite":
                    stmt = sqlite_insert(model).values(**values)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=TABLE_CONFLICT_COLS[table],
                        set_={k: v for k, v in values.items()
                              if k not in TABLE_CONFLICT_COLS[table]},
                    )
                    session.execute(stmt)
                else:
                    self._generic_upsert(session, model, table, values)
                session.commit()
            logger.debug("save_snapshot: {} 写入成功 (pk={})", table,
                         {k: values[k] for k in TABLE_CONFLICT_COLS[table]})
            return True
        except Exception:
            logger.exception("save_snapshot: 写入 {} 失败", table)
            return False

    @staticmethod
    def _generic_upsert(session: Session, model: Type, table: str, values: dict) -> None:
        """非 PG/SQLite 方言的通用 upsert: 按主键查询, 存在则更新, 否则插入。"""
        pk_filters = [
            getattr(model, col) == values[col] for col in TABLE_CONFLICT_COLS[table]
        ]
        row = session.execute(select(model).where(*pk_filters)).scalar_one_or_none()
        if row is None:
            session.add(model(**values))
        else:
            for key, val in values.items():
                setattr(row, key, val)

    # ------------------------------------------------------------------
    # 查询 / 差分
    # ------------------------------------------------------------------
    def get_latest_snapshot(self, product_id: str) -> dict:
        """返回指定商品最近一次快照 (snapshot_date 最大) 的 dict。

        Returns:
            dict: 含所有列的字典 (float 化数值); 无记录时返回 {}。
        """
        try:
            with self._session_factory() as session:
                row = session.execute(
                    select(ProductSnapshot)
                    .where(ProductSnapshot.product_id == product_id)
                    .order_by(ProductSnapshot.snapshot_date.desc())
                    .limit(1)
                ).scalar_one_or_none()
            if row is None:
                logger.debug("get_latest_snapshot: 无商品 {} 的快照", product_id)
                return {}
            return self._row_to_dict(row)
        except Exception:
            logger.exception("get_latest_snapshot: 查询 {} 失败", product_id)
            return {}

    def calculate_growth(self, product_id: str, days: int) -> dict:
        """计算商品 GMV 增速: 最近快照 vs 约 days 天前的快照。

        差分规则:
            - "最近快照" = snapshot_date 最大的记录;
            - "days 天前快照" = snapshot_date <= (最近快照日期 - days) 的
              最新记录 (允许快照间隔不为精确 days 天, 采集周期为 2-3 天);
            - 除零采用 NULLIF 语义: past GMV 为 0 或 None 时 growth_rate
              返回 None (对应 SQL 视图 NULLIF(prev.gmv_30d_est, 0));
              无可比快照时两个值均为 None, 不做猜测。

        Args:
            product_id: 商品 ID。
            days: 回溯天数 (如 7/30)。

        Returns:
            dict: {'gmv_growth': float|None, 'growth_rate': float|None}
                gmv_growth  = latest_gmv - past_gmv (绝对增量)
                growth_rate = gmv_growth / past_gmv (小数, 0.25 即 +25%)
        """
        empty = {"gmv_growth": None, "growth_rate": None}
        if not product_id or days is None or days <= 0:
            logger.error("calculate_growth: 非法参数 product_id={}, days={}", product_id, days)
            return empty
        try:
            with self._session_factory() as session:
                latest = session.execute(
                    select(ProductSnapshot)
                    .where(ProductSnapshot.product_id == product_id)
                    .order_by(ProductSnapshot.snapshot_date.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if latest is None:
                    logger.debug("calculate_growth: 商品 {} 无任何快照", product_id)
                    return empty

                cutoff = latest.snapshot_date - timedelta(days=days)
                past = session.execute(
                    select(ProductSnapshot)
                    .where(
                        ProductSnapshot.product_id == product_id,
                        ProductSnapshot.snapshot_date <= cutoff,
                        ProductSnapshot.snapshot_date != latest.snapshot_date,
                    )
                    .order_by(ProductSnapshot.snapshot_date.desc())
                    .limit(1)
                ).scalar_one_or_none()

            if past is None:
                logger.debug(
                    "calculate_growth: 商品 {} 无 {} 天前 (<= {}) 快照",
                    product_id, days, cutoff,
                )
                return empty

            gmv_growth, growth_rate = compute_growth_values(
                latest.gmv_30d_est, past.gmv_30d_est
            )
            return {"gmv_growth": gmv_growth, "growth_rate": growth_rate}
        except Exception:
            logger.exception("calculate_growth: 计算 {} 失败", product_id)
            return empty

    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_dict(row) -> dict:
        """ORM 行 → dict, Decimal 转 float 便于下游 JSON 序列化。"""
        result = {}
        for key in sa_inspect(row).mapper.column_attrs.keys():
            val = getattr(row, key)
            if isinstance(val, Decimal):
                val = float(val)
            result[key] = val
        return result
