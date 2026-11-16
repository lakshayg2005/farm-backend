from catalog_service import __version__, internal, routes
from catalog_service.events_runtime import lifespan
from catalog_service.settings import get_catalog_settings
from farm_common.app import create_app

app = create_app(
    service_name="catalog",
    version=__version__,
    settings=get_catalog_settings(),
    lifespan=lifespan,
)

app.include_router(routes.router)
app.include_router(internal.router)
