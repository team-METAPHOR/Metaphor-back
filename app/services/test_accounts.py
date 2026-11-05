"""Program test account (login id + password) for runs that need a signed-in user.

The API seals the credential with the worker's public key (app/core/test_account_crypto.py)
and keeps only a masked login id for display; it can never decrypt what it stored.
"""

from core_engine import PolicyError, canonical_url
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import credentials
from app.core import test_account_crypto as crypto
from app.core.config import get_settings
from app.core.errors import ApiError
from app.models import AuditEvent, ProgramTestAccount
from app.models.test_account import login_applies
from app.repositories import programs as programs_repo
from app.repositories import test_accounts as repo
from app.schemas.test_account import TestAccountPut
from app.services.access import ADMIN_ROLES, READ_ROLES, require_org_role
from app.services.audit import record_audit
from app.services.intake import run_policy
from app.services.limits import enforce_rate_limit, lock_key, since
from app.services.origin_verification import origin_of

SAVE_ACTION = "program.test_account_save"
DELETE_ACTION = "program.test_account_delete"
WRITES_PER_HOUR = 10


def mask_login_id(login_id: str) -> str:
    """`qa-tester@metaphor.kr` → `qa-***@metaphor.kr`; non-email ids keep a short prefix."""
    local, at, domain = login_id.rpartition("@")
    if not at:
        local, domain = login_id, ""
    keep = 3 if len(local) >= 6 else 1 if len(local) >= 3 else 0
    return f"{local[:keep]}***{at}{domain}"


async def _program(db: AsyncSession, user_id: str, program_id: str, *roles: str):
    program = await programs_repo.get(db, program_id, for_update=roles == ADMIN_ROLES)
    if program is None:
        raise ApiError(404, "NOT_FOUND", "Not found")
    await require_org_role(db, user_id, program.owner_org_id, *roles)
    return program


def _public_key():
    pem = get_settings().test_account_public_key
    if not pem.strip():
        raise ApiError(503, "TEST_ACCOUNT_UNAVAILABLE", "테스트 계정 저장을 사용할 수 없습니다.")
    try:
        return crypto.load_public_key(pem)
    except crypto.AccountKeyError:
        raise ApiError(
            503, "TEST_ACCOUNT_UNAVAILABLE", "테스트 계정 저장을 사용할 수 없습니다."
        ) from None


async def _limit_writes(db: AsyncSession, program_id: str) -> None:
    await lock_key(db, f"test-account:{program_id}")
    await enforce_rate_limit(
        db,
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.action.in_((SAVE_ACTION, DELETE_ACTION)),
            AuditEvent.target == program_id,
            AuditEvent.created_at >= since(hours=1),
        ),
        limit=WRITES_PER_HOUR,
        code="TEST_ACCOUNT_RATE_LIMITED",
    )


def _check_login_path(revision, login_path: str) -> None:
    try:
        run_policy(revision).check_url(origin_of(revision.base_url) + login_path)
    except PolicyError as error:
        raise ApiError(
            422,
            "LOGIN_PATH_OUT_OF_SCOPE",
            "로그인 경로가 승인된 테스트 범위 밖입니다.",
            {"reason": error.code},
        ) from None


def _in_use(row: ProgramTestAccount | None, revision) -> bool:
    return (
        row is not None
        and row.login_ready
        and login_applies(revision.test_context_id, revision.session_secret_ref)
        # Engine 0.4.1 refuses credentials over HTTP, so a login there never actually runs.
        and credentials.is_https(revision.base_url)
    )


async def get_account(
    db: AsyncSession, user_id: str, program_id: str
) -> tuple[ProgramTestAccount | None, bool]:
    """The stored row and whether runs on the current scope sign in with it."""
    async with db.begin():
        program = await _program(db, user_id, program_id, *READ_ROLES)
        row = await repo.get(db, program_id)
        return row, _in_use(row, await programs_repo.get_revision(db, program))


async def is_configured(db: AsyncSession, program_id: str) -> bool:
    """For program views whose caller is already authorized."""
    async with db.begin():
        return await repo.is_configured(db, program_id)


async def put_account(
    db: AsyncSession, user_id: str, program_id: str, body: TestAccountPut
) -> tuple[ProgramTestAccount, bool]:
    async with db.begin():
        program = await _program(db, user_id, program_id, *ADMIN_ROLES)
        public_key = _public_key()
        await _limit_writes(db, program.id)
        revision = await programs_repo.get_revision(db, program)
        credentials.require_https(revision.base_url)
        for path in (body.login_path, body.submit_path, body.success_path):
            if path is not None:
                _check_login_path(revision, path)
        for path in body.unauthenticated_paths:
            # Matched exactly by the engine, so it must already be in canonical form.
            try:
                canonical = canonical_url("https://scope.invalid" + path)
            except PolicyError:
                canonical = None
            if canonical != "https://scope.invalid" + path:
                raise ApiError(
                    422, "LOGIN_CONFIG_INVALID", "로그인 상태 확인 경로 형식이 올바르지 않습니다."
                )
            _check_login_path(revision, path)
        if body.success_path is not None and body.success_path == body.login_path:
            raise ApiError(
                422, "LOGIN_CONFIG_INVALID", "로그인 성공 경로는 로그인 경로와 달라야 합니다."
            )
        login_id = body.login_id.get_secret_value()
        row = await repo.get(db, program.id, for_update=True)
        version = 1 if row is None else row.version + 1
        sealed = crypto.seal(
            public_key,
            program.id,
            version,
            crypto.Credentials(login_id=login_id, password=body.password.get_secret_value()),
        )
        values = {
            "version": version,
            "key_id": sealed.key_id,
            "wrapped_key": sealed.wrapped_key,
            "ciphertext": sealed.ciphertext,
            "login_id_masked": mask_login_id(login_id),
            "login_path": body.login_path,
            "submit_path": body.submit_path,
            "success_path": body.success_path,
            "success_selector": body.success_selector,
            "submit_selector": body.submit_selector,
            "unauthenticated_paths": list(dict.fromkeys(body.unauthenticated_paths)),
            "updated_by": user_id,
        }
        if row is None:
            row = ProgramTestAccount(program_id=program.id, **values)
            await repo.add(db, row)
        else:
            for field, value in values.items():
                setattr(row, field, value)
        await record_audit(
            db,
            actor_id=user_id,
            org_id=program.owner_org_id,
            action=SAVE_ACTION,
            target=program.id,
            detail={"version": version, "key_id": sealed.key_id, "replaced": version > 1},
        )
        await db.flush()
        await db.refresh(row)
    return row, _in_use(row, revision)


async def delete_account(db: AsyncSession, user_id: str, program_id: str) -> None:
    """Remove the stored account. Idempotent (204 even when none was stored)."""
    async with db.begin():
        program = await _program(db, user_id, program_id, *ADMIN_ROLES)
        await _limit_writes(db, program.id)
        if not await repo.remove(db, program.id):
            return  # idempotent: nothing stored
        await record_audit(
            db,
            actor_id=user_id,
            org_id=program.owner_org_id,
            action=DELETE_ACTION,
            target=program.id,
        )
