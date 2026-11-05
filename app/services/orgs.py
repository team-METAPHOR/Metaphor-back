"""Self-serve organizations: create, read, rename."""

import hashlib
import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_id
from app.models import Membership, Organization
from app.models.identity import MEMBERSHIP_ROLES
from app.repositories import orgs as repo
from app.schemas.org import OrgDetail, OrgResponse
from app.services.access import require_org_role
from app.services.audit import count_actions, record_audit
from app.services.idempotency import run_once
from app.services.limits import enforce_rate_limit, lock_key, since

ORG_CREATE = "org.create"
ORG_CREATE_DAILY_LIMIT = 3


async def create_org(
    db: AsyncSession, user_id: str, name: str, idempotency_key: str | None
) -> tuple[int, dict]:
    """Create an org with the caller as its admin. 3 per user per day (audit `org.create`)."""
    request_hash = hashlib.sha256(json.dumps({"name": name}).encode()).hexdigest()
    async with db.begin():

        async def create() -> tuple[int, dict]:
            await lock_key(db, f"{ORG_CREATE}:{user_id}")
            await enforce_rate_limit(
                db,
                count_actions(user_id, (ORG_CREATE,), since(days=1)),
                limit=ORG_CREATE_DAILY_LIMIT,
                code="ORG_CREATE_LIMITED",
                message="오늘 만들 수 있는 회사 수를 초과했습니다.",
            )
            org = Organization(id=new_id(), name=name)
            await repo.add(db, org)
            await repo.add(db, Membership(org_id=org.id, user_id=user_id, role="admin"))
            await record_audit(
                db, actor_id=user_id, org_id=org.id, action=ORG_CREATE, target=org.id
            )
            await db.refresh(org)
            body = OrgResponse(id=org.id, name=org.name, role="admin", created_at=org.created_at)
            return 201, body.model_dump(mode="json")

        return await run_once(
            db,
            user_id=user_id,
            scope="orgs:create",
            key=idempotency_key,
            request_hash=request_hash,
            fn=create,
        )


async def _detail(db: AsyncSession, org: Organization, role: str) -> OrgDetail:
    return OrgDetail(
        id=org.id,
        name=org.name,
        role=role,
        member_count=await repo.count_active_members(db, org.id),
        created_at=org.created_at,
    )


async def get_org(db: AsyncSession, user_id: str, org_id: str) -> OrgDetail:
    async with db.begin():
        role = await require_org_role(db, user_id, org_id, *MEMBERSHIP_ROLES)
        return await _detail(db, await repo.get(db, org_id), role)


async def rename_org(db: AsyncSession, user_id: str, org_id: str, name: str) -> OrgDetail:
    async with db.begin():
        role = await require_org_role(db, user_id, org_id, "admin")
        org = await repo.get(db, org_id, for_update=True)
        if org.name != name:
            org.name = name
            await db.flush()
            await record_audit(
                db, actor_id=user_id, org_id=org.id, action="org.update", target=org.id
            )
        return await _detail(db, org, role)
