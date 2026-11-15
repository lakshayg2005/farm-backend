"""Base model for request/response bodies: camelCase JSON, snake_case Python."""

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class ApiModel(BaseModel):
    """Serialises `home_lat` as `homeLat`; accepts either spelling on input.

    FastAPI uses the aliases for responses automatically, so every API speaks camelCase,
    matching the error format and the event payloads.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)
