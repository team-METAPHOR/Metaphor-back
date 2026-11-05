from datetime import datetime
from typing import Annotated

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
