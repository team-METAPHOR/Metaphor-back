import io
from dataclasses import replace

import pytest
from PIL import Image
from sqlalchemy import func, select
from starlette.datastructures import UploadFile

from app.core.config import get_settings
from app.core.errors import ApiError
from app.models import Artifact, AuditEvent, Report, ValidationJob
from app.models.program import ProgramScopeRevision
from app.services import intake
from app.services.intake import (
    ReportSubmission,
    check_target_url,
    prepare_screenshot,
    read_upload_capped,
)
from app.services.reports import create_report
from tests.factories import make_org, make_program, make_user


def png(width=4, height=4, color=(255, 255, 255)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), color).save(out, format="PNG")
    return out.getvalue()


def gif_animated() -> bytes:
    out = io.BytesIO()
    frames = [Image.new("RGB", (4, 4), c) for c in ((255, 0, 0), (0, 255, 0))]
    frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:])
    return out.getvalue()


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    root = tmp_path / "storage"
    monkeypatch.setattr(get_settings(), "storage_root", root)
    return root


@pytest.fixture
async def world(db_session):
    org = await make_org(db_session)
    user = await make_user(db_session, org, "admin")
    program = await make_program(
        db_session, org, excluded_path_prefixes=["/admin"], allowed_path_prefixes=["/search", "/"]
    )
    await db_session.commit()
    return org, user, program


def submission(program, user=None, **kwargs):
    values = {
        "program": program,
        "target_url": "https://staging.example.com/search?q=cat",
        "description": "검색 결과가 없어요",
        "source": "platform" if user else "widget",
        "author_id": user.id if user else None,
    }
    values.update(kwargs)
    return ReportSubmission(**values)


async def revision(db_session, program):
    return await db_session.get(ProgramScopeRevision, (program.id, 1))


# --- upload / screenshot ---------------------------------------------------


async def test_read_upload_capped_413():
    upload = UploadFile(io.BytesIO(b"x" * 11), filename="a.png")
    assert await read_upload_capped(UploadFile(io.BytesIO(b"x" * 10)), max_bytes=10) == b"x" * 10
    with pytest.raises(ApiError) as error:
        await read_upload_capped(upload, max_bytes=10)
    assert (error.value.status, error.value.code) == (413, "UPLOAD_TOO_LARGE")


def test_prepare_screenshot_masks_region():
    shot = prepare_screenshot(png(), [[0, 0, 2, 2]])
    image = Image.open(io.BytesIO(shot.data)).convert("RGB")
    assert image.getpixel((1, 1)) == (0, 0, 0)
    assert image.getpixel((3, 3)) == (255, 255, 255)


@pytest.mark.parametrize(
    ("raw", "boxes", "code"),
    [
        (gif_animated(), None, "INVALID_SCREENSHOT"),
        (b"not an image", None, "INVALID_SCREENSHOT"),
        (png(), [[0, 0, 5, 5]], "MASK_BOX_INVALID"),
        (png(), [[0, 0, 1]], "MASK_BOX_INVALID"),
        (png(), [[0, 0, 1.5, 2]], "MASK_BOX_INVALID"),
        (png(), [[0, 0, 1, 1]] * 21, "MASK_BOX_INVALID"),
    ],
)
def test_prepare_screenshot_rejects(raw, boxes, code):
    with pytest.raises(ApiError) as error:
        prepare_screenshot(raw, boxes)
    assert (error.value.status, error.value.code) == (422, code)


# --- target url ------------------------------------------------------------


async def test_check_target_url_in_scope_and_fragment_dropped(db_session, world):
    _, _, program = world
    rev = await revision(db_session, program)
    url, stripped = check_target_url(rev, "https://staging.example.com/search?q=cat#top")
    assert (url, stripped) == ("https://staging.example.com/search?q=cat", False)


async def test_check_target_url_strips_sensitive_query(db_session, world):
    _, _, program = world
    rev = await revision(db_session, program)
    url, stripped = check_target_url(rev, "https://staging.example.com/search?token=abc&q=1")
    assert (url, stripped) == ("https://staging.example.com/search", True)


@pytest.mark.parametrize(
    ("query", "expected", "stripped"),
    [
        ("?name=x&phone=010-1234-5678", "?name=x", True),
        ("?phone=%2B82-10-1234-5678&q=cat", "?q=cat", True),
        ("?rrn=900101-1234567", "", True),
        ("?mail=a%40b.com&page=2", "?page=2", True),
        ("?q=cat&date=2026-11-05&id=a3f2c4e5b6d7890112345678901234ab", None, False),
        ("?token=abc&phone=010-1234-5678&q=1", "", True),
        ("?010-1234-5678=1&q=1", "?q=1", True),  # PII in the key
        ("?q=1&p=010%252D1234%252D5678", "?q=1", True),  # double-encoded value
        ("?q=%E0%A4%A&p=1", "", True),  # undecodable: fail closed
    ],
)
async def test_check_target_url_drops_pii_query_values(
    db_session, world, query, expected, stripped
):
    _, _, program = world
    rev = await revision(db_session, program)
    url, was_stripped = check_target_url(rev, f"https://staging.example.com/search{query}")
    expected = query if expected is None else expected
    assert (url, was_stripped) == (f"https://staging.example.com/search{expected}", stripped)


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://evil.example.com/search", "ORIGIN_OUT_OF_SCOPE"),
        ("https://staging.example.com/admin/users", "PATH_EXCLUDED"),
        ("ftp://staging.example.com/", "INVALID_URL"),
    ],
)
async def test_check_target_url_out_of_scope_422(db_session, world, url, reason):
    _, _, program = world
    rev = await revision(db_session, program)
    with pytest.raises(ApiError) as error:
        check_target_url(rev, url)
    assert (error.value.status, error.value.code) == (422, "SCOPE_VIOLATION")
    assert error.value.detail == {"reason": reason}


# --- create_report ---------------------------------------------------------


async def test_create_report_with_masked_screenshot(db_session, world, storage):
    org, user, program = world
    raw = png()
    shot = prepare_screenshot(raw, [[0, 0, 2, 2]])
    async with db_session.begin():
        report, job = await create_report(db_session, submission(program, user, screenshot=shot))
    assert job.kind == "initial" and job.status == "queued"
    assert report.latest_job_id == job.id
    artifact = await db_session.get(Artifact, report.input_artifact_id)
    assert artifact.kind == "input_image" and artifact.visibility == "reporter"
    assert artifact.sha256 == shot.sha256
    path = storage / artifact.storage_key
    assert path.read_bytes() == shot.data
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    # The unmasked upload never reaches disk.
    unmasked = prepare_screenshot(raw, None).sha256
    assert not [p for p in storage.rglob("*") if unmasked in p.name]
    audit = await db_session.scalar(select(AuditEvent).where(AuditEvent.action == "report.create"))
    assert audit.target == report.id


async def test_create_report_redacts_text(db_session, world):
    _, user, program = world
    sub = submission(program, user, description="password=hunter2 a@b.com Bearer abc.def")
    async with db_session.begin():
        report, _ = await create_report(db_session, sub)
    for secret in ("hunter2", "a@b.com", "abc.def"):
        assert secret not in report.description


async def test_create_report_widget_gets_receipt_and_org_visibility(db_session, world):
    _, _, program = world
    sub = submission(program, screenshot=prepare_screenshot(png(), None), origin_kind="mapped")
    async with db_session.begin():
        report, _ = await create_report(db_session, sub)
    assert report.author_id is None
    assert report.public_receipt_id.startswith("rcpt_")
    assert report.origin_kind == "mapped"
    artifact = await db_session.get(Artifact, report.input_artifact_id)
    assert artifact.visibility == "org"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"description": "   "}, "SCREENSHOT_OR_DESCRIPTION_REQUIRED"),
        ({"description": "x" * 8001}, "TEXT_TOO_LONG"),
        ({"title": "x" * 201}, "TEXT_TOO_LONG"),
        ({"target_url": "https://evil.example.com/"}, "SCOPE_VIOLATION"),
    ],
)
async def test_create_report_validation_422(db_session, world, change, code):
    _, user, program = world
    with pytest.raises(ApiError) as error:
        async with db_session.begin():
            await create_report(db_session, submission(program, user, **change))
    assert (error.value.status, error.value.code) == (422, code)


async def test_create_report_program_gates(db_session, world):
    _, user, program = world
    program.external_ai_allowed = False
    await db_session.commit()
    with pytest.raises(ApiError) as error:
        async with db_session.begin():
            await create_report(db_session, submission(program, user))
    assert error.value.code == "EXTERNAL_AI_NOT_ALLOWED"
    await db_session.refresh(program)
    await db_session.refresh(user)
    program.external_ai_allowed = True
    program.status = "paused"
    await db_session.commit()
    with pytest.raises(ApiError) as error:
        async with db_session.begin():
            await create_report(db_session, submission(program, user))
    assert error.value.code == "PROGRAM_NOT_ACTIVE"


async def test_create_report_failure_removes_written_file(db_session, world, storage, monkeypatch):
    _, user, program = world

    async def boom(*args, **kwargs):
        raise ApiError(429, "DAILY_LIMIT_EXCEEDED", "x")

    monkeypatch.setattr("app.services.reports.create_job", boom)
    sub = submission(program, user, screenshot=prepare_screenshot(png(), None))
    with pytest.raises(ApiError):
        async with db_session.begin():
            await create_report(db_session, sub)
    assert not [p for p in storage.rglob("*") if p.is_file()]
    assert await db_session.scalar(select(func.count()).select_from(Report)) == 0
    assert await db_session.scalar(select(func.count()).select_from(ValidationJob)) == 0


def test_submission_is_frozen():
    sub = ReportSubmission(program=None, target_url="u", source="widget")
    with pytest.raises(Exception):
        sub.description = "x"  # type: ignore[misc]
    assert replace(sub, description="y").description == "y"
    assert intake.MAX_MASK_BOXES == 20
