from functools import lru_cache

from farm_common.settings import ServiceSettings


class LogisticsSettings(ServiceSettings):
    # OSRM routing engine base URL (public demo or self-hosted)
    osrm_base_url: str = "http://router.project-osrm.org"
    # DBSCAN epsilon in kilometres
    dbscan_eps_km: float = 3.0
    # What one vehicle carries, in kg (HLD section 14: a small pickup truck, about 500 kg).
    # A cluster heavier than this is split into several pools.
    partner_capacity_kg: float = 500.0
    # At most this many customer drops in one pool (HLD section 14, step 3)
    max_drops_per_pool: int = 15
    # Outbox relay and RabbitMQ
    events_enabled: bool = True
    # Internal URL of the order service (used to fetch READY_FOR_DISPATCH orders)
    order_service_url: str = "http://order:8000"
    # Nightly pooling cron hour (UTC): 20:30 UTC is 02:00 in India, and the job pools the orders
    # due that (India) day.
    pooling_cron_hour: int = 20
    pooling_cron_minute: int = 30
    # Shared-delivery batch (orders the customer placed as FLEXIBLE): packed FLEXIBLE orders are not
    # routed at once; they wait and are clustered together at this India time every day, so one
    # partner serves a whole neighbourhood. The order service reads the same two variables to tell
    # the customer when to expect it.
    batch_hour_ist: int = 10
    batch_minute_ist: int = 0
    # The batch sees many more orders than a single packed order does, so it may cluster a little
    # wider than the immediate run's dbscan_eps_km, as long as the farms to visit are close too.
    batch_dbscan_eps_km: float = 5.0
    batch_hub_eps_km: float = 30.0
    # How a new pool gets its delivery partner:
    #   "offer"  - offer it to nearby online partners; the first to accept gets it (default)
    #   "assign" - assign it straight to the least-loaded partner (the old behaviour)
    dispatch_mode: str = "offer"
    # Pool the orders as soon as a farmer packs one (order.ready_for_dispatch), not only nightly
    pool_on_ready: bool = True
    # Wait this long after a packed order before pooling, so orders packed together share a pool
    pool_on_ready_delay_s: float = 5.0
    # Offer to partners whose latest location ping is this recent and this close to the first pickup
    offer_online_minutes: int = 30
    offer_radius_km: float = 15.0
    # Shown to partners as the time to answer; the pool stays open until someone accepts it
    offer_ttl_s: int = 120


@lru_cache
def get_logistics_settings() -> LogisticsSettings:
    return LogisticsSettings()
