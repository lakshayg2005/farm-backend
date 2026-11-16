from functools import lru_cache

from farm_common.settings import ServiceSettings


class CatalogSettings(ServiceSettings):
    # Publish events through the outbox relay and consume order/quality events (needs RabbitMQ).
    events_enabled: bool = True
    db_pool_size: int = 10


@lru_cache
def get_catalog_settings() -> CatalogSettings:
    return CatalogSettings()
