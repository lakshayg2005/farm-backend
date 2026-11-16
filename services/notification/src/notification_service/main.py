from farm_common.app import create_app
from notification_service import __version__
from notification_service.routes import router

app = create_app(service_name="notification", version=__version__)
app.include_router(router)
