from farm_common.app import create_app
from farm_common.settings import get_settings
from identity_service import __version__, admin, internal, routes
from identity_service.events_runtime import lifespan
from identity_service.settings import get_identity_settings

app = create_app(
    service_name="identity",
    version=__version__,
    settings=get_identity_settings(),
    lifespan=lifespan,
)
# farm_common.auth verifies tokens with get_settings(); identity derives its public key itself
app.dependency_overrides[get_settings] = get_identity_settings

app.include_router(routes.router)
app.include_router(admin.router)
app.include_router(internal.router)
