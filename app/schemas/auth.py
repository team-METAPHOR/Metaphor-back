from pydantic import BaseModel, Field

EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
# Upper bound keeps scrypt work per request bounded.
PASSWORD_MAX = 256


class SignupRequest(BaseModel):
    email: str = Field(max_length=320, pattern=EMAIL_PATTERN)
    password: str = Field(min_length=10, max_length=PASSWORD_MAX)
    display_name: str | None = Field(default=None, max_length=100)


class LoginRequest(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=PASSWORD_MAX)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(max_length=PASSWORD_MAX)
    new_password: str = Field(min_length=10, max_length=PASSWORD_MAX)


class UserResponse(BaseModel):
    id: str
    email: str
    display_name: str | None


class MembershipResponse(BaseModel):
    org_id: str
    org_name: str
    role: str


class MeResponse(UserResponse):
    memberships: list[MembershipResponse]
