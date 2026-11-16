from farm_common.app import create_app
from order_service import __version__
from order_service.routes import router

app = create_app(service_name="order", version=__version__)
app.include_router(router)
