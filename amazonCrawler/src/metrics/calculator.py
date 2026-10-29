"""派生指标计算器 — MetricsCalculator（只读，不写任何表）。

基于 asin_snapshots / offer_snapshots 快照数据计算 6 个业务指标：
    price_change / bsr_growth / review_velocity /
    availability_downtime / buybox_flips / asin_report + surging_products

除零/缺数据策略（与 SnapshotDiffEngine NULLIF 语义对齐）:
    一律返回 None，语义为"不可计算"，不做 0 兜底。
    例外：变化量为 0 是合法计算结果（如价格持平 → change=0.0）。

依赖注入:
    MetricsCalculator(session_factory=None, engine=None)
    - session_factory: 测试注入绑定 SQLite 内存库的 sessionmaker；
    - engine: 已构造好的 SnapshotDiffEngine（注入时优先使用）；
    - 都不传则惰性构造全局 SnapshotDiffEngine（PG 生产）。
"""

from datetime import date, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from src.common.logger import get_logger
from src.database.models import AsinSnapshot, OfferSnapshot
from src.database.snapshot_engine import SnapshotDiffEngine

logger = get_logger("metrics.calculator")

# 缺数据时的统一 None 结果（price/bsr/review 类指标共用形状）
_NULL = {"latest": None, "past": None, "change": None, "change_rate": None}


def _past_row_and_latest(session, model, asin, days):
    """取最新快照行 + days 天前（<= latest 日期 - days）的快照行。

    对齐 SnapshotDiffEngine.calculate_growth 的取数口径：
    latest = snapshot_date 最大；past = snapshot_date <= cutoff 且 != latest 日期中最大。
    返回 (latest_row, past_row)，缺任一为 None。
    """
    latest_row = session.execute(
        select(model)
        .where(model.asin == asin)
        .order_by(model.snapshot_date.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest_row is None:
        return None, None
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
        return latest_row, None
    return latest_row, past_row


def _bsr_first_rank(bsr_ranks) -> Optional[int]:
    """从 bsr_ranks JSON 提取首条 rank（BSR 是 JSON，不走数值差分）。"""
    if not bsr_ranks:
        return None
    try:
        first = bsr_ranks[0]
        rank = first.get("rank") if isinstance(first, dict) else None
        return int(rank) if rank is not None else None
    except (IndexError, TypeError, ValueError):
        return None


class MetricsCalculator:
    """只读派生指标计算器（快照数据 → 业务指标）。"""

    def __init__(self, session_factory: Optional[sessionmaker] = None,
                 engine: Optional[SnapshotDiffEngine] = None):
        if engine is not None:
            self._engine = engine
            self._session_factory = engine._session_factory
        elif session_factory is not None:
            self._session_factory = session_factory
            self._engine = SnapshotDiffEngine(session_factory=session_factory)
        else:
            # 惰性走全局 SessionLocal（import 时不触发连接创建）
            self._engine = SnapshotDiffEngine()
            self._session_factory = self._engine._session_factory

    # ------------------------------------------------------------------
    # 1. 价格变化（复用 SnapshotDiffEngine.calculate_growth）
    # ------------------------------------------------------------------
    def price_change(self, asin: str, days: int) -> dict:
        """价格差分 {latest, past, change, change_rate}，不可算全 None。"""
        try:
            return self._engine.calculate_growth(asin, days, field="price_current")
        except Exception:
            logger.exception("price_change: 计算 {} 失败", asin)
            return dict(_NULL)

    # ------------------------------------------------------------------
    # 2. BSR 增长（JSON 字段，取首条 rank 对比）
    # ------------------------------------------------------------------
    def bsr_growth(self, asin: str, days: int) -> dict:
        """BSR 变化：两次快照 bsr_ranks 首条 rank 对比（负 change = 排名变好）。"""
        empty = dict(_NULL)
        if not asin or not days or days <= 0:
            return empty
        try:
            with self._session_factory() as session:
                latest_row, past_row = _past_row_and_latest(
                    session, AsinSnapshot, asin, days)
            if latest_row is None or past_row is None:
                return empty
            latest = _bsr_first_rank(latest_row.bsr_ranks)
            past = _bsr_first_rank(past_row.bsr_ranks)
            if latest is None or past is None:
                return empty
            change = latest - past
            # BSR 分母语义上非 0；past==0 视为脏数据不可算
            if past == 0:
                return {"latest": latest, "past": past,
                        "change": None, "change_rate": None}
            return {"latest": latest, "past": past,
                    "change": change, "change_rate": change / past}
        except Exception:
            logger.exception("bsr_growth: 计算 {} 失败", asin)
            return empty

    # ------------------------------------------------------------------
    # 3. 评论增速（rating_count 差分 → 每日均值）
    # ------------------------------------------------------------------
    def review_velocity(self, asin: str, days: int) -> Optional[float]:
        """日均新增评论数；除零/缺快照/字段缺失 → None。"""
        if not asin or not days or days <= 0:
            return None
        try:
            with self._session_factory() as session:
                latest_row, past_row = _past_row_and_latest(
                    session, AsinSnapshot, asin, days)
            if latest_row is None or past_row is None:
                return None
            latest, past = latest_row.rating_count, past_row.rating_count
            if latest is None or past is None:
                return None
            delta = latest - past
            span_days = (latest_row.snapshot_date - past_row.snapshot_date).days
            if span_days <= 0:
                return None
            return delta / span_days
        except Exception:
            logger.exception("review_velocity: 计算 {} 失败", asin)
            return None

    # ------------------------------------------------------------------
    # 4. 不可售天数
    # ------------------------------------------------------------------
    def availability_downtime(self, asin: str, days: int) -> int:
        """窗口内 availability 含 unavailable/不可买的快照天数；无数据返回 0。

        判定口径：availability 文本（小写化）不含 'in stock'/'available'
        即视为不可买（含 None 也视为该日不可判定 → 不计入）。
        """
        if not asin or not days or days <= 0:
            return 0
        try:
            with self._session_factory() as session:
                latest_row, _ = _past_row_and_latest(session, AsinSnapshot, asin, days)
                if latest_row is None:
                    return 0
                cutoff = latest_row.snapshot_date - timedelta(days=days)
                rows = session.execute(
                    select(AsinSnapshot.snapshot_date, AsinSnapshot.availability)
                    .where(
                        AsinSnapshot.asin == asin,
                        AsinSnapshot.snapshot_date <= latest_row.snapshot_date,
                        AsinSnapshot.snapshot_date >= cutoff,
                    )
                ).all()
            downtime = 0
            for _, availability in rows:
                if availability is None:
                    continue  # 未知状态不误判为下架
                text = str(availability).strip().lower()
                # "unavailable" 含 "available" 子串，必须先排除
                if "unavailable" in text:
                    downtime += 1
                elif "in stock" not in text and "available" not in text:
                    downtime += 1
            return downtime
        except Exception:
            logger.exception("availability_downtime: 计算 {} 失败", asin)
            return 0

    # ------------------------------------------------------------------
    # 5. BuyBox 翻转次数
    # ------------------------------------------------------------------
    def buybox_flips(self, asin: str, days: int) -> int:
        """窗口内 BuyBox 归属变化次数（按 seller_id + 日期差分）；无数据返回 0。

        取窗口内各日期的 buybox 卖家（is_buybox=True 的 seller_id），
        相邻日期归属不同即计 1 次翻转。
        """
        if not asin or not days or days <= 0:
            return 0
        try:
            with self._session_factory() as session:
                latest_row, _ = _past_row_and_latest(session, OfferSnapshot, asin, days)
                if latest_row is None:
                    return 0
                cutoff = latest_row.snapshot_date - timedelta(days=days)
                rows = session.execute(
                    select(OfferSnapshot.snapshot_date, OfferSnapshot.seller_id)
                    .where(
                        OfferSnapshot.asin == asin,
                        OfferSnapshot.is_buybox.is_(True),
                        OfferSnapshot.snapshot_date <= latest_row.snapshot_date,
                        OfferSnapshot.snapshot_date >= cutoff,
                    )
                    .order_by(OfferSnapshot.snapshot_date.asc())
                ).all()
            # 同一天多行（脏数据）取第一个出现的 seller
            by_date: dict = {}
            for snap_date, seller_id in rows:
                by_date.setdefault(snap_date, seller_id)
            ordered = [by_date[d] for d in sorted(by_date)]
            flips = sum(
                1 for i in range(1, len(ordered))
                if ordered[i] != ordered[i - 1]
            )
            return flips
        except Exception:
            logger.exception("buybox_flips: 计算 {} 失败", asin)
            return 0

    # ------------------------------------------------------------------
    # 6. 单 ASIN 汇总报告
    # ------------------------------------------------------------------
    def asin_report(self, asin: str, days: int = 30) -> dict:
        """{asin, window_days, price_change, bsr_change, review_velocity,
        availability_downtime, buybox_flips}。"""
        return {
            "asin": asin,
            "window_days": days,
            "price_change": self.price_change(asin, days),
            "bsr_change": self.bsr_growth(asin, days),
            "review_velocity": self.review_velocity(asin, days),
            "availability_downtime": self.availability_downtime(asin, days),
            "buybox_flips": self.buybox_flips(asin, days),
        }

    # ------------------------------------------------------------------
    # 7. 窗口内评论飙升商品
    # ------------------------------------------------------------------
    def surging_products(self, min_review_velocity: float,
                         window_days: int) -> list:
        """watchlist 全部 ASIN 中 review_velocity >= 阈值的列表，降序排序。

        条目形状: {asin, review_velocity}；全部为 None velocity 的 ASIN 不入选。
        """
        try:
            with self._session_factory() as session:
                asins = session.execute(
                    select(AsinSnapshot.asin).distinct()
                ).scalars().all()
            results = []
            for asin in asins:
                velocity = self.review_velocity(asin, window_days)
                if velocity is None:
                    continue
                if velocity >= min_review_velocity:
                    results.append(
                        {"asin": asin, "review_velocity": velocity})
            results.sort(key=lambda r: r["review_velocity"], reverse=True)
            return results
        except Exception:
            logger.exception("surging_products: 扫描失败")
            return []
