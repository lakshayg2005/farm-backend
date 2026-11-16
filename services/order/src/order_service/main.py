from farm_common.app import create_app
from order_service import __version__
from order_service.routes import router
from order_service.runtime import lifespan

app = create_app(service_name="order", version=__version__, lifespan=lifespan)
app.include_router(router)
