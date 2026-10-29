-- ============================================================================
-- TKCrawler 派生指标视图 (Agent I)
-- 目标库: PostgreSQL (与 src/database/migrations 建立的快照表配套)
--
-- ⚠️ 未验证声明: 本文件中的视图 DDL 尚未经真实 PostgreSQL 实例执行验证
--    (本机测试仅覆盖 SQLite + Python 侧等价实现, 见 tests/test_metrics.py;
--    PG 实测需设置 TEST_POSTGRES_URL 环境变量后运行门控测试)。
--    已知待实测点:
--      - DISTINCT ON 语法与 ORDER BY 的搭配;
--      - 分区表 product_snapshots 上的视图执行计划/性能
--        (任务卡验收: 1000 条数据查询 <100ms);
--      - ::numeric 显式类型转换 (规避 int/int 整数除法截断)。
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 视图 1: product_metrics — 每个商品"最新快照"的 3 个即时派生指标
--   content_efficiency  内容效率   = gmv_30d_est ÷ video_count_7d
--   creator_penetration 达人渗透率 = creator_count ÷ sales_30d
--   saturation_index    饱和度指数 = (creator_count × video_count_7d) ÷ gmv_30d_est
-- 除零语义: NULLIF(x, 0) → NULL (与 MetricsCalculator 返回 None 的策略一致)。
-- 飙升系数 (surge_coefficient) 依赖跨商品类目聚合与 30 天差分, 复杂度高,
-- 以 MetricsCalculator.calculate_surge_coefficient 的 Python/SQL 实现为准,
-- 不在本视图内计算。
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW product_metrics AS
SELECT DISTINCT ON (p.product_id)
    p.product_id,
    p.snapshot_date,
    p.category_l1,
    p.category_l2,
    p.category_l3,
    p.sales_30d,
    p.gmv_30d_est,
    p.creator_count,
    p.video_count_7d,
    p.gmv_30d_est / NULLIF(p.video_count_7d, 0) AS content_efficiency,
    p.creator_count::numeric
        / NULLIF(p.sales_30d, 0)                    AS creator_penetration,
    (p.creator_count * p.video_count_7d)::numeric
        / NULLIF(p.gmv_30d_est, 0)                  AS saturation_index
FROM product_snapshots AS p
ORDER BY p.product_id, p.snapshot_date DESC;


-- ----------------------------------------------------------------------------
-- 视图 2 (可选): views_growth — 视频播放量 7 天差分
--   每条视频当日快照 vs 7 天前同日快照, 计算播放量绝对增量与相对增速。
--   无 7 天前快照时 prev 侧为 NULL → 增量/增速为 NULL。
-- ----------------------------------------------------------------------------
CREATE OR REPLACE VIEW views_growth AS
SELECT
    curr.video_id,
    curr.product_id,
    curr.snapshot_date,
    curr.views,
    prev.views                                    AS views_7d_ago,
    curr.views - prev.views                       AS views_growth_7d,
    (curr.views - prev.views)::numeric
        / NULLIF(prev.views, 0)                   AS views_growth_rate_7d
FROM video_snapshots AS curr
LEFT JOIN video_snapshots AS prev
    ON prev.video_id = curr.video_id
   AND prev.snapshot_date = curr.snapshot_date - INTERVAL '7 days';
