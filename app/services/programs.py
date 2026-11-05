import re

from core_engine import BrowserOptions, PolicyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.ids import new_id
from app.models import Program, ProgramScopeRevision
from app.repositories import programs as repo
from app.repositories import test_conditions as conditions_repo
from app.schemas.program import SCOPE_FIELDS, ProgramCreate, ProgramPatch
from app.services.access import ADMIN_ROLES, org_role, require_org_role
from app.services.audit import record_audit
from app.services.intake import run_policy

# keep in sync with app.worker.resets.RESETS
ALLOWED_RESET_ADAPTERS = ("stateless",)
_APP_VERSION = re.compile(r"[A-Za-z0-9_.:-]{1,100}")
_PROGRAM_FIELDS = (
    "name",
    "status",
    "external_ai_allowed",
    "auto_fresh_retries",
)


def _not_found() -> ApiError:
    return ApiError(404, "NOT_FOUND", "Not found")


def browser_options(revision: ProgramScopeRevision) -> BrowserOptions:
    # A placeholder header stands in for the worker-side secret so the engine applies the same
    # rules it will apply at run time (e.g. a session needs a synthetic test context).
    headers = {"Cookie": "placeholder"} if revision.session_secret_ref else {}
    return BrowserOptions(
        width=revision.width,
        height=revision.height,
        app_version=revision.app_version,
        test_context_id=revision.test_context_id,
        mask_selectors=tuple(revision.mask_selectors),
        session_headers=headers,
    )


def _validated(revision: ProgramScopeRevision) -> ProgramScopeRevision:
    """Validate with the engine's own types and store its normalized URL/prefixes."""
    if revision.reset_adapter not in ALLOWED_RESET_ADAPTERS:
        raise ApiError(422, "RESET_ADAPTER_UNKNOWN", "지원하지 않는 초기화 방식입니다.")
    try:
        policy = run_policy(revision)
    except PolicyError as error:
        raise ApiError(422, error.code, "승인 범위 설정이 올바르지 않습니다.") from None
    try:
        browser_options(revision)
        if not all(_APP_VERSION.fullmatch(v) for v in revision.approved_app_versions):
            raise ValueError("invalid approved app version")
    except ValueError as error:
        raise ApiError(
            422, "INVALID_RUN_OPTIONS", "실행 설정이 올바르지 않습니다.", {"reason": str(error)}
        ) from None
    revision.base_url = policy.base_url
    revision.allowed_path_prefixes = list(policy.allowed_path_prefixes)
    revision.excluded_path_prefixes = list(policy.excluded_path_prefixes)
    return revision


_OPERATOR_FIELDS = ("session_secret_ref", "allow_private_targets", "private_cidrs")


def _scope_values(revision: ProgramScopeRevision) -> dict:
    return {field: getattr(revision, field) for field in (*SCOPE_FIELDS, *_OPERATOR_FIELDS)}


async def create_program(db: AsyncSession, user_id: str, body: ProgramCreate):
    async with db.begin():
        await require_org_role(db, user_id, body.org_id, *ADMIN_ROLES)
        program = Program(
            id=new_id(),
            owner_org_id=body.org_id,
            current_revision=1,
            **body.model_dump(include=set(_PROGRAM_FIELDS)),
        )
        revision = _validated(
            ProgramScopeRevision(
                program_id=program.id,
                revision=1,
                allow_private_targets=False,
                private_cidrs=[],
                session_secret_ref=None,
                created_by=user_id,
                **body.model_dump(include=set(SCOPE_FIELDS)),
            )
        )
        await repo.add(db, program)
        await repo.add(db, revision)
        await record_audit(
            db,
            actor_id=user_id,
            org_id=program.owner_org_id,
            action="program.create",
            target=program.id,
        )
        await db.refresh(program)
        await db.refresh(revision)
    return program, revision


async def get_program(db: AsyncSession, user_id: str, program_id: str):
    """Return (program, revision) for members of the owning org; 404 for everyone else."""
    async with db.begin():
        program = await repo.get(db, program_id)
        if program is None or await org_role(db, user_id, program.owner_org_id) is None:
            raise _not_found()
        return program, await repo.get_revision(db, program)


async def list_programs(db: AsyncSession, user_id: str):
    async with db.begin():
        return await repo.list_visible(db, user_id)


async def update_program(db: AsyncSession, user_id: str, program_id: str, body: ProgramPatch):
    changes = body.model_dump(exclude_unset=True)
    async with db.begin():
        # Row lock: concurrent scope edits must not both insert revision N+1.
        program = await repo.get(db, program_id, for_update=True)
        if program is None:
            raise _not_found()
        await require_org_role(db, user_id, program.owner_org_id, *ADMIN_ROLES)
        revision = await repo.get_revision(db, program)

        for field in _PROGRAM_FIELDS:
            if field in changes and changes[field] is None:
                raise ApiError(422, "VALIDATION_FAILED", f"{field} cannot be null")
        program_changes = {
            f: v for f, v in changes.items() if f in _PROGRAM_FIELDS and getattr(program, f) != v
        }
        scope_input = {f: v for f, v in changes.items() if f in SCOPE_FIELDS}
        for field, value in scope_input.items():
            if value is None and field != "session_secret_ref":
                raise ApiError(422, "VALIDATION_FAILED", f"{field} cannot be null")

        candidate = None
        if scope_input:
            # Old revisions are never updated: build N+1. Operator-only settings carry over,
            # except that a provisioned session is bound to its origin and is dropped when
            # base_url moves (the operator must re-bind it).
            values = {**_scope_values(revision), **scope_input}
            if "base_url" in scope_input:
                values["session_secret_ref"] = None
            candidate = ProgramScopeRevision(
                program_id=program.id,
                revision=program.current_revision + 1,
                created_by=user_id,
                **values,
            )
            candidate = _validated(candidate)
            if candidate.base_url == revision.base_url:
                candidate.session_secret_ref = revision.session_secret_ref
                _validated(candidate)  # re-check run options with the session restored
            else:
                # Condition sessions are bound to the old origin too.
                await conditions_repo.unbind_sessions(db, program.id)
            if _scope_values(candidate) == _scope_values(revision):
                candidate = None

        for field, value in program_changes.items():
            setattr(program, field, value)
        if candidate is not None:
            await repo.add(db, candidate)
            program.current_revision = candidate.revision
            revision = candidate
        if program_changes or candidate is not None:
            fields = sorted(program_changes) + ([] if candidate is None else sorted(scope_input))
            await record_audit(
                db,
                actor_id=user_id,
                org_id=program.owner_org_id,
                action="program.update",
                target=program.id,
                detail={
                    "fields": fields,
                    "revision": program.current_revision,
                    "status": program.status,
                },
            )
            await db.flush()
            await db.refresh(program)
        await db.refresh(revision)
    return program, revision
