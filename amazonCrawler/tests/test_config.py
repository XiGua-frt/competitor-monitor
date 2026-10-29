"""src/common/config.py 单测。"""

from src.common.config import Config, config


class TestConfigDefaults:
    """默认值与契约一致。"""

    def test_default_request_intervals(self):
        assert Config.REQUEST_MIN_INTERVAL == 8
        assert Config.REQUEST_MAX_INTERVAL == 15

    def test_default_quotas(self):
        assert Config.DAILY_QUOTA_ASINS == 200
        assert Config.DAILY_QUOTA_SEARCH == 50

    def test_default_cooldown_and_marketplace(self):
        assert Config.SOFT_COOLDOWN_SECONDS == 3600
        assert Config.MARKETPLACE == "www.amazon.com"

    def test_database_url_property(self):
        c = Config()
        c.POSTGRES_USER = "u"
        c.POSTGRES_PASSWORD = "p"
        c.POSTGRES_HOST = "h"
        c.POSTGRES_PORT = 5432
        c.POSTGRES_DB = "db"
        url = c.DATABASE_URL
        assert url.startswith("postgresql+psycopg2://")
        assert "u:p@h:5432/db" in url

    def test_proxy_url_disabled_by_default(self):
        c = Config()
        c.PROXY_ENABLED = False
        assert c.PROXY_URL is None
        c.PROXY_ENABLED = True
        c.PROXY_TYPE = "http"
        c.PROXY_HOST = "127.0.0.1"
        c.PROXY_PORT = 7892
        assert c.PROXY_URL == "http://127.0.0.1:7892"

    def test_global_singleton_exists(self):
        assert isinstance(config, Config)
