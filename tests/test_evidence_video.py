"""Reproduction video (WebM) and exported Playwright test: persist, link, range download."""

import hashlib
import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.integrations import storage
from app.models import Artifact, AuditEvent, ValidationJob
from app.worker import runner
from app.worker.persist import persist_result
from tests.fakes import completed_report
from tests.test_handoff_evidence import download_url, world  # noqa: F401

ENGINE = "e" * 32
WEBM = b"\x1a\x45\xdf\xa3" + bytes(range(256)) * 4  # EBML magic + 1024 bytes
SPEC = b"import { test, expect } from '@playwright/test';\n// 010-1234-5678 stays as-is\n"


def video_name(data: bytes) -> str:
    return f"video_{hashlib.sha256(data).hexdigest()}.webm"


def video_meta(attempt: int, data: bytes | None, **extra) -> dict:
    meta = {
        "attempt": attempt,
        "revision": 0,
        "plan_sha256": "ab" * 32,
        "kind": "masked_capture",
        "status": "ready" if data is not None else "failed",
        "sampling_fps": 4,
        "has_audio": False,
        "truncated": False,
        "capture_error": None if data is not None else "VIDEO_ENCODING_OR_STORAGE_FAILED",
        "artifact": None,
    }
    if data is not None:
        meta.update(
            frames_captured=12,
            start_offset_seconds=0.5,
            duration_seconds=3.0,
            artifact={
                "name": video_name(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "mime_type": "video/webm",
            },
        )
    meta.update(extra)
    return meta


def spec_export(data: bytes, sha: str | None = None) -> dict:
    return {
        "status": "ready",
        "plan_sha256": "ab" * 32,
        "artifact": {
            "name": "reproduction.spec.ts",
            "sha256": sha or hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "mime_type": "text/plain",
        },
        "required_environment": ["QA_RESET_MODULE"],
    }


def engine_dir(world):  # noqa: F811
    report = world["report"]
    return storage.safe_path(report.org_id, report.id, ENGINE)


def write_engine_file(world, name: str, data: bytes) -> None:  # noqa: F811
    storage.write_private(engine_dir(world) / name, data)


async def persist(db_session, conn, world, *, videos, test_export, video_enabled=True):  # noqa: F811
    await db_session.execute(
        update(ValidationJob).where(ValidationJob.id == world["job_id"]).values(status="running")
    )
    await db_session.commit()
    write_engine_file(world, "result.json", b"{}")
    report = completed_report(ENGINE)
    report.update(videos=videos, video_enabled=video_enabled, test_export=test_export)
    raw = {"status": "completed", "error_code": None, "questions": [], "report": report}
    assert persist_result(
        conn,
        world["job_id"],
        raw,
        org_id=world["report"].org_id,
        report_id=world["report"].id,
        engine_dir=engine_dir(world),
        retention_days=30,
    )


async def job_artifacts(db_session, world) -> dict[str, Artifact]:  # noqa: F811
    db_session.expire_all()
    rows = await db_session.scalars(select(Artifact).where(Artifact.job_id == world["job_id"]))
    return {a.kind: a for a in rows}


async def media(client, world) -> dict:  # noqa: F811
    url = f"/api/v1/validation-jobs/{world['job_id']}/artifacts"
    response = await client.get(url, headers=world["dev"])
    assert response.status_code == 200, response.text
    return response.json()["media"]


async def ready_video(db_session, conn, world, data: bytes = WEBM) -> str:  # noqa: F811
    write_engine_file(world, video_name(data), data)
    await persist(db_session, conn, world, videos=[video_meta(1, data)], test_export=None)
    return (await job_artifacts(db_session, world))["video"].id


# --- persist ---------------------------------------------------------------------------


async def test_persist_keeps_video_and_spec_bytes_and_hashes(db_session, conn, world):  # noqa: F811
    write_engine_file(world, video_name(WEBM), WEBM)
    write_engine_file(world, "reproduction.spec.ts", SPEC)
    await persist(
        db_session, conn, world, videos=[video_meta(1, WEBM)], test_export=spec_export(SPEC)
    )
    rows = await job_artifacts(db_session, world)
    for kind, data in (("video", WEBM), ("playwright_spec", SPEC)):
        row = rows[kind]
        assert (row.sha256, row.size) == (hashlib.sha256(data).hexdigest(), len(data))
        # Untouched on disk: no text redaction of the executable test or the video.
        assert (storage.storage_root() / row.storage_key).read_bytes() == data


async def test_hash_mismatch_is_not_registered(db_session, conn, world, caplog):  # noqa: F811
    tampered = WEBM + b"x"
    write_engine_file(world, video_name(WEBM), tampered)  # name/metadata say WEBM
    write_engine_file(world, "reproduction.spec.ts", SPEC)
    caplog.set_level(logging.WARNING)
    await persist(
        db_session,
        conn,
        world,
        videos=[video_meta(1, WEBM)],
        test_export=spec_export(SPEC, sha="0" * 64),
    )
    rows = await job_artifacts(db_session, world)
    assert "video" not in rows and "playwright_spec" not in rows
    assert "hash mismatch" in caplog.text and video_name(WEBM) not in caplog.text


async def test_media_links_videos_and_test_export(client, db_session, conn, world):  # noqa: F811
    write_engine_file(world, video_name(WEBM), WEBM)
    write_engine_file(world, "reproduction.spec.ts", SPEC)
    videos = [
        video_meta(1, WEBM, truncated=True, capture_error="VIDEO_FRAME_BUDGET"),
        video_meta(2, None),
    ]
    await persist(db_session, conn, world, videos=videos, test_export=spec_export(SPEC))
    rows = await job_artifacts(db_session, world)
    body = await media(client, world)
    assert body["video_enabled"] is True
    assert body["trace_available"] is False
    first, second = body["videos"]
    assert first == {
        "attempt": 1,
        "revision": 0,
        "plan_sha256": "ab" * 32,
        "status": "ready",
        "truncated": True,
        "capture_error": "VIDEO_FRAME_BUDGET",
        "has_audio": False,
        "sampling_fps": 4,
        "frames_captured": 12,
        "start_offset_seconds": 0.5,
        "duration_seconds": 3.0,
        "artifact_id": rows["video"].id,
    }
    assert (second["status"], second["artifact_id"]) == ("failed", None)
    assert second["capture_error"] == "VIDEO_ENCODING_OR_STORAGE_FAILED"
    assert body["test_export"] == {
        "status": "ready",
        "plan_sha256": "ab" * 32,
        "artifact_id": rows["playwright_spec"].id,
        "required_environment": ["QA_RESET_MODULE"],
        "error_code": None,
    }
    # Engine file names are never handed out.
    url = f"/api/v1/validation-jobs/{world['job_id']}/artifacts"
    assert video_name(WEBM) not in (await client.get(url, headers=world["dev"])).text


async def test_media_video_disabled_differs_from_failed(client, db_session, conn, world):  # noqa: F811
    await persist(db_session, conn, world, videos=[], test_export=None, video_enabled=False)
    body = await media(client, world)
    assert (body["video_enabled"], body["videos"], body["test_export"]) == (False, [], None)


async def test_media_video_unavailable_without_encoder(client, db_session, conn, world):  # noqa: F811
    videos = [video_meta(1, None, status="unavailable", capture_error="VIDEO_ENCODER_UNAVAILABLE")]
    await persist(db_session, conn, world, videos=videos, test_export=None)
    [video] = (await media(client, world))["videos"]
    assert (video["status"], video["capture_error"], video["artifact_id"]) == (
        "unavailable",
        "VIDEO_ENCODER_UNAVAILABLE",
        None,
    )


async def test_media_pending_while_job_active(client, world):  # noqa: F811
    body = await media(client, world)
    assert (body["video_enabled"], body["videos"], body["test_export"]) == (None, [], None)


async def test_report_md_links_video_and_spec(client, db_session, conn, world):  # noqa: F811
    write_engine_file(world, video_name(WEBM), WEBM)
    write_engine_file(world, "reproduction.spec.ts", SPEC)
    md = f"[영상 보기]({video_name(WEBM)})\n[Playwright 테스트 다운로드](reproduction.spec.ts)\n"
    write_engine_file(world, "report.md", md.encode())
    await persist(
        db_session, conn, world, videos=[video_meta(1, WEBM)], test_export=spec_export(SPEC)
    )
    rows = await job_artifacts(db_session, world)
    text = (await client.get(download_url(rows["report_md"].id), headers=world["dev"])).text
    assert f"(/api/v1/evidence/{rows['video'].id}/download)" in text
    assert f"(/api/v1/evidence/{rows['playwright_spec'].id}/download)" in text


# --- download --------------------------------------------------------------------------


async def get(client, world, artifact_id, range_header=None, who="dev"):  # noqa: F811
    headers = dict(world[who])
    if range_header is not None:
        headers["Range"] = range_header
    return await client.get(download_url(artifact_id), headers=headers)


async def test_video_full_download_inline_with_accept_ranges(client, db_session, conn, world):  # noqa: F811
    artifact_id = await ready_video(db_session, conn, world)
    response = await get(client, world, artifact_id)
    assert response.status_code == 200
    assert response.content == WEBM
    assert response.headers["content-type"] == "video/webm"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-disposition"] == f'inline; filename="{artifact_id}.webm"'
    assert response.headers["x-content-type-options"] == "nosniff"
    audits = (
        await db_session.scalars(select(AuditEvent).where(AuditEvent.action == "evidence.download"))
    ).all()
    assert [a.target for a in audits] == [artifact_id]


@pytest.mark.parametrize(
    ("header", "start", "end"),
    [
        ("bytes=0-99", 0, 99),  # front
        ("bytes=1000-", 1000, len(WEBM) - 1),  # open-ended back
        ("bytes=-10", len(WEBM) - 10, len(WEBM) - 1),  # suffix
        ("bytes=500-99999", 500, len(WEBM) - 1),  # end clamped to size
    ],
)
async def test_video_range_206(client, db_session, conn, world, header, start, end):  # noqa: F811
    artifact_id = await ready_video(db_session, conn, world)
    response = await get(client, world, artifact_id, header)
    assert response.status_code == 206
    assert response.content == WEBM[start : end + 1]
    assert response.headers["content-range"] == f"bytes {start}-{end}/{len(WEBM)}"
    assert response.headers["content-length"] == str(end - start + 1)
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-type"] == "video/webm"


@pytest.mark.parametrize(
    "header",
    ["bytes=5000-", "bytes=10-5", "bytes=-0", "bytes=0-1,5-6", "items=0-1", "bytes=a-b", "bytes="],
)
async def test_video_invalid_range_416(client, db_session, conn, world, header):  # noqa: F811
    artifact_id = await ready_video(db_session, conn, world)
    response = await get(client, world, artifact_id, header)
    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{len(WEBM)}"
    assert response.json()["error"]["code"] == "RANGE_NOT_SATISFIABLE"


async def test_video_other_org_and_expired_404(client, db_session, conn, world):  # noqa: F811
    artifact_id = await ready_video(db_session, conn, world)
    for who in ("other_admin", "stranger"):
        assert (await get(client, world, artifact_id, "bytes=0-1", who)).status_code == 404
    await db_session.execute(
        update(Artifact)
        .where(Artifact.id == artifact_id)
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await db_session.commit()
    assert (await get(client, world, artifact_id, "bytes=0-1")).status_code == 404
    assert (await get(client, world, artifact_id)).status_code == 404


async def test_spec_download_is_safe_text_attachment(client, db_session, conn, world):  # noqa: F811
    write_engine_file(world, "reproduction.spec.ts", SPEC)
    await persist(db_session, conn, world, videos=[], test_export=spec_export(SPEC))
    artifact_id = (await job_artifacts(db_session, world))["playwright_spec"].id
    response = await get(client, world, artifact_id, "bytes=0-1")  # Range ignored for text
    assert response.status_code == 200
    assert response.content == SPEC
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert (
        response.headers["content-disposition"] == f'attachment; filename="{artifact_id}.spec.ts"'
    )
    assert response.headers["x-content-type-options"] == "nosniff"


# --- worker settings -------------------------------------------------------------------


def snapshot() -> dict:
    return {
        "program_id": "p",
        "scope_revision": 1,
        "base_url": "https://staging.example.com",
        "allowed_path_prefixes": ["/"],
        "excluded_path_prefixes": [],
        "width": 1280,
        "height": 800,
        "app_version": None,
        "test_context_id": "ctx",
        "org_id": "o",
    }


@pytest.mark.parametrize(("value", "expected"), [(None, False), ("0", False), ("1", True)])
def test_record_video_follows_operator_flag(monkeypatch, tmp_path, value, expected):
    if value is None:
        monkeypatch.delenv("QA_VIDEO_ENABLED", raising=False)
    else:
        monkeypatch.setenv("QA_VIDEO_ENABLED", value)
    service = runner.build_service(
        snapshot(), runner.JobStore(tmp_path), model=None, app_version="1"
    )
    assert service.options.record_video is expected


def test_service_uses_raised_browser_budget(tmp_path):
    # Engine default (2 concurrent) aborts a page that loads HTML, JS and CSS in parallel.
    service = runner.build_service(
        snapshot(), runner.JobStore(tmp_path), model=None, app_version="1"
    )
    assert service.browser_limits.max_concurrent_requests == 4
    assert service.browser_limits.max_total_requests == 300
    assert service.browser_limits.max_download_bytes == 32 * 1024 * 1024
    # The 40,000 default ran out before plan revision on busy pages with reporter activity.
    assert service.limits.max_reported_tokens == 100_000


def test_encoder_check_logs_error_code_when_missing(monkeypatch, caplog):
    monkeypatch.setenv("QA_VIDEO_ENABLED", "true")
    monkeypatch.setattr(runner, "find_encoder", lambda: None)
    caplog.set_level(logging.ERROR)
    assert runner.check_video_encoder() is False
    assert "VIDEO_ENCODER_UNAVAILABLE" in caplog.text


def test_encoder_check_ok_or_disabled(monkeypatch, caplog):
    monkeypatch.setenv("QA_VIDEO_ENABLED", "1")
    monkeypatch.setattr(runner, "find_encoder", lambda: "/usr/bin/ffmpeg")
    assert runner.check_video_encoder() is True
    monkeypatch.delenv("QA_VIDEO_ENABLED")
    monkeypatch.setattr(runner, "find_encoder", lambda: None)
    caplog.set_level(logging.ERROR)
    assert runner.check_video_encoder() is True
    assert caplog.text == ""
