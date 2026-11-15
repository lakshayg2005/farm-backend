import re
from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator, EmailStr, Field, StringConstraints, field_validator

from farm_common.auth import Role
from farm_common.schemas import ApiModel

# Roles a user can give themselves; inspector and admin are granted by an admin only.
SELF_SERVICE_ROLES = frozenset({Role.FARMER, Role.CUSTOMER, Role.DELIVERY})

_PHONE = re.compile(r"^\+[1-9]\d{9,14}$")


def normalize_phone(value: str) -> str:
    """Accept '98765 43210', '09876543210' or '+91 98765-43210'; store E.164 ('+919876543210')."""
    digits = re.sub(r"[\s\-()]", "", value)
    if re.fullmatch(r"0?\d{10}", digits):
        digits = "+91" + digits[-10:]
    if not _PHONE.fullmatch(digits):
        raise ValueError("Enter a valid mobile number, e.g. +919876543210")
    return digits


Phone = Annotated[str, AfterValidator(normalize_phone)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
# bcrypt only uses the first 72 bytes
Password = Annotated[str, StringConstraints(min_length=8, max_length=72)]
OtpCodeStr = Annotated[str, StringConstraints(pattern=r"^\d{6}$")]


class RegisterIn(ApiModel):
    name: Name
    phone: Phone
    email: EmailStr | None = None
    password: Password
    roles: list[Role] = Field(min_length=1)
    home_lat: float | None = Field(default=None, ge=-90, le=90)
    home_lng: float | None = Field(default=None, ge=-180, le=180)

    @field_validator("roles")
    @classmethod
    def only_self_service_roles(cls, roles: list[Role]) -> list[Role]:
        if not set(roles) <= SELF_SERVICE_ROLES:
            raise ValueError("You can only register as farmer, customer or delivery")
        return sorted(set(roles))


class UserOut(ApiModel):
    id: UUID
    name: str
    phone: str
    email: str | None
    roles: list[Role]
    phone_verified: bool
    farmer_verified: bool
    home_lat: float | None
    home_lng: float | None
    created_at: datetime


class TokenOut(ApiModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


class LoginIn(ApiModel):
    phone: Phone
    password: str = Field(max_length=72)


class OtpRequestIn(ApiModel):
    phone: Phone


class OtpRequestOut(ApiModel):
    expires_in: int
    resend_after: int
    # Only filled in dev mode (mock SMS), so frontends can complete the flow without a phone.
    dev_code: str | None = None


class OtpVerifyIn(ApiModel):
    phone: Phone
    code: OtpCodeStr


class RefreshIn(ApiModel):
    refresh_token: str


class UpdateMeIn(ApiModel):
    name: Name | None = None
    email: EmailStr | None = None
    home_lat: float | None = Field(default=None, ge=-90, le=90)
    home_lng: float | None = Field(default=None, ge=-180, le=180)


class AddRoleIn(ApiModel):
    role: Role

    @field_validator("role")
    @classmethod
    def only_self_service(cls, role: Role) -> Role:
        if role not in SELF_SERVICE_ROLES:
            raise ValueError("Only an admin can grant this role")
        return role


class SetRolesIn(ApiModel):
    roles: list[Role] = Field(min_length=1)


class UserPage(ApiModel):
    items: list[UserOut]
    next_cursor: str | None


class PublicKeyOut(ApiModel):
    algorithm: str = "RS256"
    issuer: str
    audience: str
    public_key: str
