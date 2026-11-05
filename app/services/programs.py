import re

from core_engine import BrowserOptions, PolicyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import credentials
from app.core.errors import ApiError
from app.core.ids import new_id
from app.integrations.state_changes import state_change_policy
from app.models import Program, ProgramScopeRevision
from app.repositories import programs as repo
from app.repositories import test_accounts as test_accounts_repo
from app.repositories import test_conditions as conditions_repo
from app.schemas.program import SCOPE_FIELDS, ProgramCreate, ProgramPatch
from app.services.access import ADMIN_ROLES, org_role, require_org_role
from app.services.audit import record_audit
from app.services.intake import run_policy

# keep in sync with app.worker.resets.RESETS
ALLOWED_RESET_ADAPTERS = ("stateless", "buglab-v1")
# Registered adapters that really restore server-side test data: the only ones that may back
# approved writes (`state_changes`). `stateless` never qualifies (the engine refuses it too).
# keep in sync with app.worker.resets.STATEFUL
STATEFUL_RESET_ADAPTERS: tuple[str, ...] = ("buglab-v1",)
# Never writable from the worker browser, whatever the approval says: the test-control API
# (the reset adapter calls it server-side) and account creation.
RESERVED_WRITE_PREFIXES = ("/api/test",)
_ACCOUNT_CREATION = re.compile(r"(?:^|/)(?:sign-?up|register)(?:/|$)", re.IGNORECASE)
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


def _overlaps(path: str, prefix: str) -> bool:
    path, prefix = path.rstrip("/") or "/", prefix.rstrip("/") or "/"
    return any(
        a == b or a.startswith(b.rstrip("/") + "/") for a, b in ((path, prefix), (prefix, path))
    )


def _check_state_changes(revision: ProgramScopeRevision) -> None:
    """Backend rules on top of the engine's own checks (run in `run_policy`)."""
    approval = revision.state_changes
    if approval["reset_adapter_id"] not in STATEFUL_RESET_ADAPTERS:
        raise ApiError(
            422, "STATE_RESET_REQUIRED", "쓰기 승인에는 실제 서버 초기화 어댑터가 필요합니다."
        )
    if approval["reset_adapter_id"] != revision.reset_adapter:
        raise ApiError(
            422,
            "STATE_RESET_MISMATCH",
            "쓰기 승인의 초기화 어댑터와 프로그램 초기화 방식이 같아야 합니다.",
        )
    if revision.session_secret_ref is not None:
        # The reset ends the account's sessions: a fixed operator Cookie would die on every
        # attempt. Writes use the saved test account (reset → fresh login) instead.
        raise ApiError(
            422,
            "STATE_CHANGES_SESSION_CONFLICT",
            "운영자 세션이 연결된 범위에는 쓰기를 승인할 수 없습니다. "
            "테스트 계정 로그인을 사용하세요.",
        )
    try:
        # The engine's own rules first (environment, ≤32 rules, write methods, no `/`).
        rules = state_change_policy(approval).allowed_requests
    except PolicyError as error:
        raise ApiError(422, error.code, "쓰기 승인 설정이 올바르지 않습니다.") from None
    for _, path in rules:
        if _ACCOUNT_CREATION.search(path) or any(
            _overlaps(path, reserved) for reserved in RESERVED_WRITE_PREFIXES
        ):
            raise ApiError(422, "STATE_CHANGE_PATH_RESERVED", "쓰기를 승인할 수 없는 경로입니다.")


def _validated(revision: ProgramScopeRevision) -> ProgramScopeRevision:
    """Validate with the engine's own types and store its normalized URL/prefixes."""
    if revision.reset_adapter not in ALLOWED_RESET_ADAPTERS:
        raise ApiError(422, "RESET_ADAPTER_UNKNOWN", "지원하지 않는 초기화 방식입니다.")
    if revision.state_changes is not None:
        _check_state_changes(revision)
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
    if policy.state_changes is not None:
        revision.state_changes = policy.state_changes.public()
    return revision


_OPERATOR_FIELDS = ("session_secret_ref", "allow_private_targets", "private_cidrs")


def _scope_values(revision: ProgramScopeRevision) -> dict:
    return {field: getattr(revision, field) for field in (*SCOPE_FIELDS, *_OPERATOR_FIELDS)}


async def _has_credentials(db: AsyncSession, program_id: str, revision) -> bool:
    """An operator session, a saved test account, or a condition session exists."""
    return (
        revision.session_secret_ref is not None
        or await test_accounts_repo.is_configured(db, program_id)
        or await conditions_repo.has_sessions(db, program_id)
    )


def credentials_blocked_by_http(revision: ProgramScopeRevision, has_test_account: bool) -> bool:
    """Stored credentials (legacy data) that core_engine 0.4.1 will refuse on this HTTP origin."""
    return not credentials.is_https(revision.base_url) and (
        revision.session_secret_ref is not None or has_test_account
    )


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
            if value is None and field not in ("session_secret_ref", "state_changes"):
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
                if not credentials.is_https(candidate.base_url) and await _has_credentials(
                    db, program.id, revision
                ):
                    # Never silently drop and never keep credentials for an HTTP origin:
                    # remove them first (or stay on HTTPS).
                    credentials.require_https(candidate.base_url)
                # Condition sessions are bound to the old origin too.
                await conditions_repo.unbind_sessions(db, program.id)
                # So is a saved test account: never send its credentials to a new site.
                if await test_accounts_repo.remove(db, program.id):
                    await record_audit(
                        db,
                        actor_id=user_id,
                        org_id=program.owner_org_id,
                        action="program.test_account_delete",
                        target=program.id,
                        detail={"reason": "base_url_changed"},
                    )
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
