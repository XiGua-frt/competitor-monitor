"""派生指标计算器 — MetricsCalculator (Agent I)。

职责:
    基于已入库的 product_snapshots 快照数据, 只读计算 4 个业务派生指标
    (本模块不采集数据、不写任何表):

    1. content_efficiency   内容效率   = gmv_30d_est ÷ video_count_7d
    2. creator_penetration  达人渗透率 = creator_count ÷ sales_30d
    3. saturation_index     饱和度指数 = (creator_count × video_count_7d) ÷ gmv_30d_est
    4. surge_coefficient    飙升系数   = 商品 30d GMV 增速 ÷ 类目平均 30d GMV 增速

除零策略 (全模块统一, 与 SnapshotDiffEngine 的 NULLIF 语义一致):
    - 分母为 0 或缺失 (None)、所需快照数据不可得 → 一律返回 None,
      语义为"无法计算"。不返回 0 兜底: 0 是一个合法的计算结果,
      会污染下游的均值/排序类统计, 与 SQL 视图 NULLIF(x, 0) → NULL 对齐;
    - 例外: 分子为 0 而分母正常 → 返回 0.0 (合法计算结果,
      如 gmv_30d_est=0 时的 content_efficiency、增速持平时的 surge 系数)。

依赖注入:
    本模块不自行创建连接池。MetricsCalculator 接收 sessionmaker
    (缺省复用 src.database.connection.SessionLocal), 并可注入
    SnapshotDiffEngine 实例 (surge 系数的商品增速来自其
    calculate_growth 契约方法)。
"""

from datetime import timedelta
from typing import Optional

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import sessionmaker

from src.common.logger import get_logger
from src.database.models import ProductSnapshot
from src.database.snapshot_engine import SnapshotDiffEngine

logger = get_logger("metrics.calculator")

# 飙升系数的增速回溯窗口 (天), 与 SnapshotDiffEngine.calculate_growth 对齐
SURGE_WINDOW_DAYS = 30

# calculate_surge_coefficient 的 category 参数与这三列任一相等即视为同类目
# (调用方可传大类 "Home"、中类 "Lighting" 或细类 "LED Strips")
CATEGORY_COLUMNS = ("category_l1", "category_l2", "category_l3")

# 类目平均增速的 SQL: 对类目内每个商品取"最新快照 GMV"与"约 30 天前快照 GMV"
# 做差分求 growth_rate, 再 AVG 聚合。
# 说明:
#   - 子查询 t 中 latest/past 用相关标量子查询实现 (LIMIT 1 语法 SQLite/PG 通用);
#   - NULLIF(past_gmv, 0) 使 past 为 0/NULL 的商品在 AVG 中被自动剔除
#     (AVG 忽略 NULL), 与除零策略一致;
#   - `* 1.0` 规避 SQLite 整数除法截断 (PG 下 numeric 除法不受影响, 属无害写法);
#   - cutoff 由 Python 侧计算后以 ISO 字符串绑定, 日期比较在
#     SQLite (TEXT 'YYYY-MM-DD') 与 PG (date) 下语义一致。
_CATEGORY_AVG_GROWTH_SQL = text("""
    SELECT AVG(
        (t.latest_gmv - t.past_gmv) * 1.0 / NULLIF(t.past_gmv, 0)
    )
    FROM (
        SELECT p.product_id,
               (SELECT s2.gmv_30d_est
                  FROM product_snapshots AS s2
                 WHERE s2.product_id = p.product_id
                 ORDER BY s2.snapshot_date DESC
                 LIMIT 1) AS latest_gmv,
               (SELECT s3.gmv_30d_est
                  FROM product_snapshots AS s3
                 WHERE s3.product_id = p.product_id
                   AND s3.snapshot_date <= :cutoff
                   AND s3.snapshot_date < (
                        SELECT MAX(s4.snapshot_date)
                          FROM product_snapshots AS s4
                         WHERE s4.product_id = p.product_id)
                 ORDER BY s3.snapshot_date DESC
                 LIMIT 1) AS past_gmv
          FROM product_snapshots AS p
         WHERE p.category_l1 = :category
            OR p.category_l2 = :category
            OR p.category_l3 = :category
         GROUP BY p.product_id
    ) AS t
""")


class MetricsCalculator:
    """4 个派生指标的只读计算器。

    Args:
        session_factory: 会话工厂 (sessionmaker); 缺省使用全局
            SessionLocal, 不新建连接池。测试时注入绑定 SQLite
            内存库的 sessionmaker。
        diff_engine: 可选注入的 SnapshotDiffEngine; 缺省时用
            session_factory 构造一个 (surge 系数的商品增速来自
            其 calculate_growth 契约)。
    """

    def __init__(
        self,
        session_factory: Optional[sessionmaker] = None,
        diff_engine: Optional[SnapshotDiffEngine] = None,
    ):
        self._session_factory = session_factory
        self._diff = diff_engine or SnapshotDiffEngine(
            session_factory=session_factory
        )

    # ------------------------------------------------------------------
    # 指标 1-3: 基于最新快照的即时指标
    # ------------------------------------------------------------------
    def calculate_content_efficiency(self, product_id: str) -> Optional[float]:
        """内容效率 = gmv_30d_est ÷ video_count_7d (取商品最新快照)。

        除零策略: video_count_7d 为 0/None, 或快照不可得 → None。
        分子 gmv_30d_est 为 0 且分母正常 → 0.0。
        """
        vals = self._latest_snapshot_values(product_id)
        if vals is None:
            return None
        gmv, videos = vals["gmv_30d_est"], vals["video_count_7d"]
        if not videos:  # None 或 0
            return None
        if gmv is None:
            return None
        return gmv / videos

    def calculate_creator_penetration(self, product_id: str) -> Optional[float]:
        """达人渗透率 = creator_count ÷ sales_30d (取商品最新快照)。

        除零策略: sales_30d 为 0/None, 或 creator_count/快照不可得 → None。
        分子 creator_count 为 0 且分母正常 → 0.0。
        """
        vals = self._latest_snapshot_values(product_id)
        if vals is None:
            return None
        creators, sales = vals["creator_count"], vals["sales_30d"]
        if creators is None:
            return None
        if not sales:  # None 或 0
            return None
        return creators / sales

    def calculate_saturation_index(self, product_id: str) -> Optional[float]:
        """饱和度指数 = (creator_count × video_count_7d) ÷ gmv_30d_est。

        除零策略: gmv_30d_est 为 0/None → None (分母);
        creator_count / video_count_7d 任一缺失 → None;
        二者为 0 但 gmv 正常 → 0.0 (合法结果, 表示无内容供给)。
        """
        vals = self._latest_snapshot_values(product_id)
        if vals is None:
            return None
        creators, videos, gmv = (
            vals["creator_count"],
            vals["video_count_7d"],
            vals["gmv_30d_est"],
        )
        if creators is None or videos is None:
            return None
        if not gmv:  # None 或 0 (分母)
            return None
        return (creators * videos) / gmv

    # ------------------------------------------------------------------
    # 指标 4: 飙升系数 (类目相对增速)
    # ------------------------------------------------------------------
    def calculate_surge_coefficient(
        self, product_id: str, category: str
    ) -> Optional[float]:
        """飙升系数 = 商品 30d GMV 增速 ÷ 类目平均 30d GMV 增速。

        定义:
            - 商品增速: SnapshotDiffEngine.calculate_growth(product_id, 30)
              的 growth_rate (相对增速, 0.25 即 +25%);
            - 类目平均: SQL 聚合 — 对 category 匹配 (category_l1/l2/l3
              任一相等) 的所有商品, 各自做同样的 30d 差分取 growth_rate,
              再 AVG (past 为 0/NULL 或无可比快照的商品自动剔除,
              AVG 忽略 NULL)。类目平均包含本商品自身。

        除零策略: 类目平均为 0/None、商品增速不可得 (None)、
        类目下无数据 → None; 商品增速为 0 而类目平均正常 → 0.0。
        类目平均为负时照常计算 (衰退类目中出现正增长商品 → 系数为负)。

        Returns:
            float | None
        """
        if not product_id or not category:
            logger.error(
                "calculate_surge_coefficient: 非法参数 product_id={!r}, category={!r}",
                product_id, category,
            )
            return None

        growth = self._diff.calculate_growth(product_id, SURGE_WINDOW_DAYS)
        product_rate = growth.get("growth_rate")
        if product_rate is None:
            logger.debug(
                "surge: 商品 {} 的 30d 增速不可得, 无法计算飙升系数", product_id
            )
            return None

        cat_avg = self._category_avg_growth(category)
        if cat_avg is None:
            logger.debug("surge: 类目 {} 平均增速不可得", category)
            return None
        if cat_avg == 0:
            logger.debug("surge: 类目 {} 平均增速为 0, 除零返回 None", category)
            return None
        return product_rate / cat_avg

    def _category_avg_growth(self, category: str) -> Optional[float]:
        """类目平均 30d GMV 增速 (SQL 聚合, 见模块级 _CATEGORY_AVG_GROWTH_SQL)。

        窗口基准日取类目内全局最新快照日 (快照按批次统一采集, 各商品
        最新快照日一致), cutoff = 基准日 - 30 天。无法取得基准日或
        AVG 结果为 NULL (类目内无可比商品) → None。
        """
        with self._session_factory() as session:
            max_date = session.execute(
                select(func.max(ProductSnapshot.snapshot_date)).where(
                    or_(
                        ProductSnapshot.category_l1 == category,
                        ProductSnapshot.category_l2 == category,
                        ProductSnapshot.category_l3 == category,
                    )
                )
            ).scalar()
            if max_date is None:
                logger.debug("category_avg_growth: 类目 {} 无任何快照", category)
                return None
            cutoff = (max_date - timedelta(days=SURGE_WINDOW_DAYS)).isoformat()
            avg = session.execute(
                _CATEGORY_AVG_GROWTH_SQL,
                {"cutoff": cutoff, "category": category},
            ).scalar()
        return float(avg) if avg is not None else None

    # ------------------------------------------------------------------
    # 批量: 一次算齐 4 指标
    # ------------------------------------------------------------------
    def calculate_all(
        self, product_id: str, category: Optional[str] = None
    ) -> dict:
        """一次计算 4 个指标, 返回 dict。

        Args:
            product_id: 商品 ID。
            category: 飙升系数使用的类目; 缺省时从商品最新快照的
                category_l2 推断 (依次回退 l1/l3, 取第一个非 None 值),
                与 _category_avg_growth 的三列 OR 匹配语义兼容。

        Returns:
            dict: {
                'content_efficiency':  float | None,
                'creator_penetration': float | None,
                'saturation_index':    float | None,
                'surge_coefficient':   float | None,
            }
            各指标独立的除零/缺数语义见对应方法 docstring。
        """
        result = {
            "content_efficiency": self.calculate_content_efficiency(product_id),
            "creator_penetration": self.calculate_creator_penetration(product_id),
            "saturation_index": self.calculate_saturation_index(product_id),
        }
        if category is None:
            category = self._infer_category(product_id)
        result["surge_coefficient"] = (
            self.calculate_surge_coefficient(product_id, category)
            if category
            else None
        )
        return result

    def _infer_category(self, product_id: str) -> Optional[str]:
        """从商品最新快照推断类目: category_l2 → l1 → l3 第一个非 None 值。"""
        with self._session_factory() as session:
            row = session.execute(
                select(
                    ProductSnapshot.category_l2,
                    ProductSnapshot.category_l1,
                    ProductSnapshot.category_l3,
                )
                .where(ProductSnapshot.product_id == product_id)
                .order_by(ProductSnapshot.snapshot_date.desc())
                .limit(1)
            ).first()
        if row is None:
            return None
        return next((c for c in row if c), None)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _latest_snapshot_values(self, product_id: str) -> Optional[dict]:
        """取最新快照中参与指标计算的 4 个字段 (float 化)。

        Returns:
            dict | None: 无任何快照时返回 None。
        """
        with self._session_factory() as session:
            row = session.execute(
                select(
                    ProductSnapshot.gmv_30d_est,
                    ProductSnapshot.video_count_7d,
                    ProductSnapshot.creator_count,
                    ProductSnapshot.sales_30d,
                )
                .where(ProductSnapshot.product_id == product_id)
                .order_by(ProductSnapshot.snapshot_date.desc())
                .limit(1)
            ).first()
        if row is None:
            logger.debug("latest_snapshot_values: 商品 {} 无快照", product_id)
            return None

        def _to_float(v):
            return None if v is None else float(v)

        gmv, videos, creators, sales = row
        return {
            "gmv_30d_est": _to_float(gmv),
            "video_count_7d": videos,
            "creator_count": creators,
            "sales_30d": sales,
        }
