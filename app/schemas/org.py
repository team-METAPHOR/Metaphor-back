from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

Role = Literal["developer", "reviewer", "admin"]
OrgName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class OrgCreate(BaseModel):
    name: OrgName


class OrgPatch(BaseModel):
    name: OrgName


class OrgResponse(BaseModel):
    id: str
    name: str
    role: str
    created_at: datetime


class OrgDetail(OrgResponse):
    member_count: int


class MemberResponse(BaseModel):
    user_id: str
    email: str
    display_name: str | None
    role: str
    is_active: bool
    joined_at: datetime


class MemberRolePatch(BaseModel):
    role: Role


class InviteCreate(BaseModel):
    role: Role
    expires_in_days: int = Field(default=7, ge=1, le=30)


class InviteCreated(BaseModel):
    """The only response that ever carries the raw token."""

    id: str
    role: str
    expires_at: datetime
    token: str


class InviteResponse(BaseModel):
    id: str
    role: str
    created_by: str
    expires_at: datetime


class InviteAccept(BaseModel):
    token: str = Field(min_length=1, max_length=200)


class InviteAccepted(BaseModel):
    org_id: str
    org_name: str
    role: str
