from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, StringConstraints

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
    role: Literal["developer", "reviewer", "admin"]
