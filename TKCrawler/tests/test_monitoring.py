"""Tests for src/monitoring/metrics.py."""

import importlib
import itertools
import sys
from pathlib import Path

import pytest
from prometheus_client import REGISTRY

# 确保项目根目录在 sys.path 中，便于以 src.monitoring 导入
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.monitoring import metrics  # noqa: E402

_uid_counter = itertools.count()


@pytest.fixture()
def uid():
    """每个用例唯一的标签后缀，隔离用例间的计数器状态。"""
    return next(_uid_counter)


class TestRegistration:
    def test_all_metrics_registered(self):
        for name in (
            metrics.REQUESTS_TOTAL,
            metrics.DURATION_SECONDS,
            metrics.WAF_BLOCKS_TOTAL,
            metrics.MISSING_FIELDS_RATIO,
        ):
            assert name in REGISTRY._names_to_collectors, name

    def test_register_metrics_idempotent(self):
        first = metrics.register_metrics()
        second = metrics.register_metrics()
        assert first["requests_total"] is second["requests_total"]
        assert first["duration_seconds"] is second["duration_seconds"]
        assert first["waf_blocks_total"] is second["waf_blocks_total"]
        assert first["missing_fields_ratio"] is second["missing_fields_ratio"]

    def test_module_reload_no_duplicate_error(self):
        # 重新加载模块不应抛出 duplicated timeseries 异常
        importlib.reload(metrics)
        assert metrics.REQUESTS_TOTAL in REGISTRY._names_to_collectors


class TestObserveRequest:
    def test_counter_and_histogram_updated(self, uid):
        scraper = f"shop_{uid}"
        metrics.observe_request(scraper, "success", 0.42)
        assert metrics.get_request_count(scraper, "success") == 1.0

        value = REGISTRY.get_sample_value(
            metrics.DURATION_SECONDS + "_count", {"scraper": scraper}
        )
        assert value == 1.0

        value = REGISTRY.get_sample_value(
            metrics.DURATION_SECONDS + "_sum", {"scraper": scraper}
        )
        assert value == pytest.approx(0.42)

    def test_multiple_statuses_counted_independently(self, uid):
        scraper = f"shop_{uid}"
        metrics.observe_request(scraper, "success", 0.1)
        metrics.observe_request(scraper, "failed", 0.2)
        metrics.observe_request(scraper, "failed", 0.3)

        assert metrics.get_request_count(scraper, "success") == 1.0
        assert metrics.get_request_count(scraper, "failed") == 2.0

    def test_scrapers_isolated(self, uid):
        s1, s2 = f"shop_{uid}", f"video_{uid}"
        metrics.observe_request(s1, "success", 0.1)
        metrics.observe_request(s2, "success", 0.1)
        assert metrics.get_request_count(s1, "success") == 1.0
        assert metrics.get_request_count(s2, "success") == 1.0

    def test_track_request_context_manager_success(self, uid):
        scraper = f"creator_{uid}"
        with metrics.track_request(scraper):
            pass
        assert metrics.get_request_count(scraper, "success") == 1.0

    def test_track_request_context_manager_failure(self, uid):
        scraper = f"creator_{uid}"
        with pytest.raises(RuntimeError):
            with metrics.track_request(scraper):
                raise RuntimeError("boom")
        assert metrics.get_request_count(scraper, "failed") == 1.0


class TestWafBlocks:
    def test_inc_waf_block(self, uid):
        s1, s2 = f"shop_{uid}", f"video_{uid}"
        metrics.inc_waf_block(s1)
        metrics.inc_waf_block(s1)
        metrics.inc_waf_block(s2)
        assert metrics.get_waf_block_count(s1) == 2.0
        assert metrics.get_waf_block_count(s2) == 1.0

    def test_exposed_sample_value(self, uid):
        scraper = f"shop_{uid}"
        metrics.inc_waf_block(scraper)
        value = REGISTRY.get_sample_value(
            metrics.WAF_BLOCKS_TOTAL, {"scraper": scraper}
        )
        assert value == 1.0


class TestMissingRatio:
    def test_set_missing_ratio(self, uid):
        table = f"product_snapshots_{uid}"
        metrics.set_missing_ratio(table, 0.35)
        assert metrics.get_missing_ratio(table) == pytest.approx(0.35)

    def test_ratio_value_visible_in_registry(self, uid):
        table = f"video_snapshots_{uid}"
        metrics.set_missing_ratio(table, 0.12)
        value = REGISTRY.get_sample_value(
            metrics.MISSING_FIELDS_RATIO, {"table": table}
        )
        assert value == pytest.approx(0.12)

    def test_ratio_overwrites(self, uid):
        table = f"product_snapshots_{uid}"
        metrics.set_missing_ratio(table, 0.5)
        metrics.set_missing_ratio(table, 0.1)
        assert metrics.get_missing_ratio(table) == pytest.approx(0.1)
