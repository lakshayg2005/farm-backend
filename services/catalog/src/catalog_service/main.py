from catalog_service import __version__, internal, routes
from farm_common.app import create_app

app = create_app(service_name="catalog", version=__version__)

app.include_router(routes.router)
app.include_router(internal.router)
