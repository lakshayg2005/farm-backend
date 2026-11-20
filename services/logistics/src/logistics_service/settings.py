from functools import lru_cache

from farm_common.settings import ServiceSettings


class LogisticsSettings(ServiceSettings):
    # OSRM routing engine base URL (public demo or self-hosted)
    osrm_base_url: str = "http://router.project-osrm.org"
    # DBSCAN epsilon in kilometres
    dbscan_eps_km: float = 3.0
    # Minimum pool capacity in kg (split cluster when exceeded)
    partner_capacity_kg: float = 50.0
    # At most this many customer drops in one pool (HLD section 14, step 3)
    max_drops_per_pool: int = 15
    # Outbox relay and RabbitMQ
    events_enabled: bool = True
    # Internal URL of the order service (used to fetch READY_FOR_DISPATCH orders)
    order_service_url: str = "http://order:8000"
    # Nightly pooling cron hour (UTC)
    pooling_cron_hour: int = 20  # 20:30 UTC ≈ 02:00 IST
    pooling_cron_minute: int = 30


@lru_cache
def get_logistics_settings() -> LogisticsSettings:
    return LogisticsSettings()
