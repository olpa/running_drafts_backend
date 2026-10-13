from pydantic import UUID4, BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelCaseRequest(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        validate_by_alias=True,
        validate_by_name=False,
    )


class CamelCaseResponse(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        serialize_by_alias=True,
        validate_by_alias=True,
        validate_by_name=True,
    )


__all__ = ["CamelCaseRequest", "CamelCaseResponse", "UUID4"]
