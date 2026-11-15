from farm_common.app import create_app
from media_service import __version__, internal, routes
from media_service.events_runtime import lifespan
from media_service.settings import get_media_settings

app = create_app(
    service_name="media",
    version=__version__,
    settings=get_media_settings(),
    lifespan=lifespan,
)

app.include_router(internal.router)
app.include_router(routes.router)
