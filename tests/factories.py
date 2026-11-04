"""Small seed helpers shared by tests. Each returns flushed ORM rows."""

from app.core.ids import new_id
from app.models import (
    Membership,
    Organization,
    Program,
    ProgramScopeRevision,
    Report,
    User,
)


async def make_org(session, name="Acme"):
    org = Organization(id=new_id(), name=name)
    session.add(org)
    await session.flush()
    return org


async def make_user(session, org=None, role=None, email=None):
    user = User(id=new_id(), email=email or f"{new_id()}@example.com", password_hash="x")
    session.add(user)
    await session.flush()
    if org is not None and role is not None:
        session.add(Membership(org_id=org.id, user_id=user.id, role=role))
        await session.flush()
    return user


async def make_program(session, org, *, daily_job_limit=50, **revision):
    program = Program(
        id=new_id(),
        owner_org_id=org.id,
        name="Shop",
        status="active",
        external_ai_allowed=True,
        daily_job_limit=daily_job_limit,
    )
    session.add(program)
    await session.flush()
    values = {
        "base_url": "https://staging.example.com",
        "allowed_path_prefixes": ["/"],
        "test_context_id": "ctx-1",
        "reset_adapter": "stateless",
    }
    values.update(revision)
    session.add(ProgramScopeRevision(program_id=program.id, revision=1, **values))
    await session.flush()
    return program


async def make_report(session, org, program, author=None, **kwargs):
    values = {
        "id": new_id(),
        "program_id": program.id,
        "org_id": org.id,
        "source": "platform" if author else "widget",
        "author_id": author.id if author else None,
        "target_url": "https://staging.example.com/",
        "description": "bug",
    }
    values.update(kwargs)
    report = Report(**values)
    session.add(report)
    await session.flush()
    return report
