"""快照差分引擎 — SnapshotDiffEngine（Agent A 正式实现）。

对采集层（Agent C scrapers / Agent D scheduler）暴露的接口契约:
    save_snapshot(data: dict, table: str) -> bool      # upsert, 白名单三张表
    get_latest_snapshot(asin, table='asin_snapshots') -> dict | None
    calculate_growth(asin, days, field='price_current') -> dict

calculate_growth 契约语义（重点）:
    返回 {'latest': float|None, 'past': float|None,
          'change': float|None, 'change_rate': float|None}
    None = 不可计算（除零 / 缺快照 / 字段缺失），**不做 0 兜底**:
      - asin 无任何快照            → 全 None
      - 无 days 天前的可比快照      → 全 None
      - 任一快照字段为 None         → 全 None
      - past == 0                  → change=None, change_rate=None（除零）
      - 正常                       → change = latest - past,
                                     change_rate = change / past
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Optional, Type

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from src.common.logger import get_logger
from src.database import connection as _db_connection
from src.database.models import (
    AsinSnapshot,
    KeywordRankSnapshot,
    OfferSnapshot,
)

logger = get_logger("database.snapshot_engine")

# table 参数白名单 → ORM 模型映射
TABLE_MODELS: dict[str, Type] = {
    "asin_snapshots": AsinSnapshot,
    "keyword_rank_snapshots": KeywordRankSnapshot,
    "offer_snapshots": OfferSnapshot,
}

# 各表 upsert 冲突目标（即主键列）+ 实体键（get_latest_snapshot 按它过滤）
TABLE_CONFLICT_COLS: dict[str, list] = {
    "asin_snapshots": ["asin", "snapshot_date"],
    "keyword_rank_snapshots": ["keyword", "asin", "snapshot_date"],
    "offer_snapshots": ["asin", "seller_id", "snapshot_date"],
}

# calculate_growth / get_latest_snapshot 只对 asin 维度有意义
TABLE_ENTITY_KEY: dict[str, str] = {
    "asin_snapshots": "asin",
    "keyword_rank_snapshots": "asin",
    "offer_snapshots": "asin",
}


def _coerce_value(column: Any, value: Any) -> Any:
    """按列类型做基础收敛：字符串日期/时间 → date/datetime；Decimal 兼容 str。"""
    if value is None:
        return None
    py_type = column.type.python_type
    if py_type is not str and isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            if py_type is date:
                return date.fromisoformat(s[:10])
        except (ValueError, TypeError):
            pass
        try:
            if py_type is Decimal:
                return Decimal(s)
            if py_type is int and "." not in s:
                return int(s)
            if py_type is float:
                return float(s)
            if py_type is bool:
                return s.lower() in ("1", "true", "yes", "on")
        except (ValueError, TypeError, ArithmeticError):
            return None
    return value


class SnapshotDiffEngine:
    """快照写入 + 差分计算引擎。

    Args:
        session_factory: 会话工厂，默认用全局惰性 SessionLocal（PG 生产）；
            测试注入绑定 SQLite 内存库的 sessionmaker。
    """

    def __init__(self, session_factory: Optional[sessionmaker] = None):
        # 延迟取全局 SessionLocal：import 不触发 engine 创建，首次实例化才建
        self._session_factory = session_factory or _db_connection.SessionLocal

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def save_snapshot(self, data: dict, table: str) -> bool:
        """按 upsert 语义写入一条快照（主键冲突时更新其余字段）。

        Returns:
            bool: 写入成功 True；data 非法 / 表名不在白名单 / 缺主键 / DB 异常 False。
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
            values: dict[str, Any] = {}
            for key, val in data.items():
                if key in column_names:
                    values[key] = _coerce_value(mapper.columns[key], val)
                else:
                    # 未识别字段忽略（快照 dict 可能带 scraper 元数据）
                    logger.debug("save_snapshot: 忽略非法字段 '{}' (table={})", key, table)

            conflict_cols = TABLE_CONFLICT_COLS[table]
            missing_pk = [pk for pk in conflict_cols
                          if pk not in values or values[pk] is None]
            if missing_pk:
                logger.error("save_snapshot: 缺少主键字段 {} (table={})", missing_pk, table)
                return False

            with self._session_factory() as session:
                dialect = session.bind.dialect.name
                if dialect == "postgresql":
                    stmt = pg_insert(model).values(**values)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=conflict_cols,
                        set_={k: v for k, v in values.items() if k not in conflict_cols},
                    )
                    session.execute(stmt)
                elif dialect == "sqlite":
                    stmt = sqlite_insert(model).values(**values)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=conflict_cols,
                        set_={k: v for k, v in values.items() if k not in conflict_cols},
                    )
                    session.execute(stmt)
                else:
                    self._generic_upsert(session, model, table, values)
                session.commit()
            logger.debug("save_snapshot: {} 写入成功 (pk={})", table,
                         {k: values[k] for k in conflict_cols})
            return True
        except Exception:
            logger.exception("save_snapshot: 写入 {} 失败", table)
            return False

    @staticmethod
    def _generic_upsert(session: Session, model: Type, table: str, values: dict) -> None:
        """非 PG/SQLite 方言的通用 upsert：按主键查询，存在则更新，否则插入。"""
        conflict_cols = TABLE_CONFLICT_COLS[table]
        pk_filters = [getattr(model, col) == values[col] for col in conflict_cols]
        row = session.execute(select(model).where(*pk_filters)).scalar_one_or_none()
        if row is None:
            session.add(model(**values))
        else:
            for key, val in values.items():
                setattr(row, key, val)

    # ------------------------------------------------------------------
    # 查询 / 差分
    # ------------------------------------------------------------------
    def get_latest_snapshot(self, asin: str,
                            table: str = "asin_snapshots") -> Optional[dict]:
        """返回指定 asin 最近一次快照（snapshot_date 最大）。

        Returns:
            dict | None: 含所有列的 dict（Decimal → float 便于 JSON 序列化）；
                asin 无记录或表名非法时返回 None。
        """
        model = TABLE_MODELS.get(table)
        if model is None:
            logger.error("get_latest_snapshot: 非法表名 '{}'", table)
            return None
        entity_key = TABLE_ENTITY_KEY[table]
        try:
            with self._session_factory() as session:
                row = session.execute(
                    select(model)
                    .where(getattr(model, entity_key) == asin)
                    .order_by(model.snapshot_date.desc())
                    .limit(1)
                ).scalar_one_or_none()
            if row is None:
                logger.debug("get_latest_snapshot: {} 无 {} 的快照", table, asin)
                return None
            return self._row_to_dict(row)
        except Exception:
            logger.exception("get_latest_snapshot: 查询 {}({}) 失败", asin, table)
            return None

    def calculate_growth(self, asin: str, days: int,
                         field: str = "price_current") -> dict:
        """计算 asin 某字段在约 days 天内的增速（对比最近快照 vs days 天前快照）。

        差分规则:
            - latest = snapshot_date 最大的记录的 field 值;
            - past   = snapshot_date <= (latest 日期 - days) 的最新记录的 field 值
              （允许快照间隔不为精确 days 天，采集周期 2-3 天）;
            - None 语义见模块 docstring，不做 0 兜底。

        Returns:
            {'latest': float|None, 'past': float|None,
             'change': float|None, 'change_rate': float|None}
        """
        empty = {"latest": None, "past": None, "change": None, "change_rate": None}
        if not asin or days is None or days <= 0:
            logger.error("calculate_growth: 非法参数 asin={}, days={}", asin, days)
            return empty
        model = TABLE_MODELS["asin_snapshots"]
        field_col = getattr(model, field, None)
        if field_col is None:
            logger.error("calculate_growth: 未知字段 '{}'", field)
            return empty
        try:
            with self._session_factory() as session:
                latest_row = session.execute(
                    select(model)
                    .where(model.asin == asin)
                    .order_by(model.snapshot_date.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if latest_row is None:
                    logger.debug("calculate_growth: asin {} 无任何快照", asin)
                    return empty

                latest_val = getattr(latest_row, field)
                cutoff = latest_row.snapshot_date - timedelta(days=days)
                past_row = session.execute(
                    select(model)
                    .where(
                        model.asin == asin,
                        model.snapshot_date <= cutoff,
                        model.snapshot_date != latest_row.snapshot_date,
                    )
                    .order_by(model.snapshot_date.desc())
                    .limit(1)
                ).scalar_one_or_none()

            if past_row is None:
                logger.debug("calculate_growth: asin {} 无 {} 天前 (<= {}) 快照",
                             asin, days, cutoff)
                return empty

            past_val = getattr(past_row, field)
            if latest_val is None or past_val is None:
                # 字段缺失 → 不可算，不兜底
                logger.debug("calculate_growth: asin {} 字段 {} 存在缺失", asin, field)
                return empty

            latest_f = float(latest_val)
            past_f = float(past_val)
            change = latest_f - past_f
            if past_f == 0:
                # 除零 → None（NULLIF 语义）
                return {"latest": latest_f, "past": past_f,
                        "change": None, "change_rate": None}
            return {"latest": latest_f, "past": past_f,
                    "change": change, "change_rate": change / past_f}
        except Exception:
            logger.exception("calculate_growth: 计算 {}({}) 失败", asin, field)
            return empty

    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_dict(row) -> dict:
        """ORM 行 → dict，Decimal → float 便于下游 JSON 序列化。"""
        result = {}
        for key in sa_inspect(row).mapper.column_attrs.keys():
            val = getattr(row, key)
            if isinstance(val, Decimal):
                val = float(val)
            result[key] = val
        return result
