import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.core.ids import new_id
from app.integrations import storage
from app.models import Artifact, AuditEvent, QaResult, Report, ValidationJob
from tests.factories import login_as, make_org, make_program
from tests.fakes import completed_report
from tests.test_core_intake import png

ANALYSIS = {
    "classification": "functional",
    "confidence": "medium",
    "classification_reason": "r",
    "severity": "high",
    "severity_reason": "r",
    "summary": "빈 검색 결과 처리 누락 <script>alert(1)</script>",
    "hypotheses": [{"description": "캐시 미갱신", "source_refs": ["run:0"]}],
    "solutions": [
        {
            "title": "빈 결과 분기 추가",
            "description": "https://evil.example 참고",
            "source_refs": ["run:0"],
            "verification_steps": ["missing 검색"],
            "tradeoffs": ["추가 렌더링"],
        }
    ],
    "missing_information": ["서버 로그"],
}


@pytest.fixture
async def world(client, db_session):
    org = await make_org(db_session)
    program = await make_program(db_session, org)
    await db_session.commit()
    _, admin = await login_as(client, db_session, org, "admin")
    _, dev = await login_as(client, db_session, org, "developer")
    _, stranger = await login_as(client, db_session)
    other_org = await make_org(db_session, name="B")
    _, other_admin = await login_as(client, db_session, other_org, "admin")
    response = await client.post(
        "/api/v1/reports",
        data={
            "program_id": program.id,
            "target_url": "https://staging.example.com/search",
            "description": "검색 결과가 이상해요",
        },
        files={"screenshot": ("s.png", png(), "image/png")},
        headers=dev,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    report = await db_session.get(Report, body["id"])
    return {
        "org": org,
        "report": report,
        "job_id": body["validation_job_id"],
        "admin": admin,
        "dev": dev,
        "stranger": stranger,
        "other_admin": other_admin,
    }


async def write_artifact(db_session, world, name: str, data: bytes, kind: str, **extra) -> str:
    report = world["report"]
    path = storage.safe_path(report.org_id, report.id, "e" * 32, name)
    storage.write_private(path, data)
    artifact_id = new_id()
    db_session.add(
        Artifact(
            id=artifact_id,
            org_id=report.org_id,
            report_id=report.id,
            job_id=world["job_id"],
            kind=kind,
            storage_key=storage.relative_key(path),
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            visibility="org",
            expires_at=extra.pop("expires_at", datetime.now(UTC) + timedelta(days=30)),
            **extra,
        )
    )
    await db_session.commit()
    return artifact_id


async def finish(db_session, world, *, analysis=ANALYSIS, status="completed"):
    report = completed_report("e" * 32, analysis=False)
    report["analysis"] = analysis
    await db_session.execute(
        update(ValidationJob)
        .where(ValidationJob.id == world["job_id"])
        .values(
            status=status,
            verdict="reproduced",
            classification="functional",
            analysis_available=analysis is not None,
            finished_at=datetime.now(UTC),
        )
    )
    db_session.add(QaResult(job_id=world["job_id"], raw={"status": status, "report": report}))
    await db_session.commit()


def handoff_url(world) -> str:
    return f"/api/v1/reports/{world['report'].id}/developer-handoff"


def download_url(artifact_id: str) -> str:
    return f"/api/v1/evidence/{artifact_id}/download"


async def test_handoff_pending_while_job_active(client, world):
    response = await client.get(handoff_url(world), headers=world["dev"])
    assert response.status_code == 200
    body = response.json()
    assert (body["state"], body["job_id"], body["ai_generated"]) == (
        "pending",
        world["job_id"],
        True,
    )
    assert body["solutions"] == [] and body["summary"] is None


async def test_handoff_ready_maps_solutions(client, db_session, world):
    await finish(db_session, world)
    response = await client.get(handoff_url(world), headers=world["dev"])
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "state": "ready",
        "job_id": world["job_id"],
        "verdict": "reproduced",
        "summary": ANALYSIS["summary"],  # plain text, returned as-is (never rendered)
        "hypotheses": ANALYSIS["hypotheses"],
        "solutions": ANALYSIS["solutions"],
        "missing_information": ["서버 로그"],
        "severity": "high",
        "confidence": "medium",
        "classification": "functional",
        "ai_generated": True,
    }
    assert response.headers["content-type"].startswith("application/json")


async def test_handoff_unavailable_when_analysis_null(client, db_session, world):
    await finish(db_session, world, analysis=None)
    body = (await client.get(handoff_url(world), headers=world["admin"])).json()
    assert body["state"] == "unavailable"
    assert body["verdict"] == "reproduced"
    assert (body["summary"], body["severity"], body["confidence"]) == (None, None, None)
    assert body["hypotheses"] == body["solutions"] == body["missing_information"] == []


async def test_handoff_unavailable_for_failed_job_without_result(client, db_session, world):
    await db_session.execute(
        update(ValidationJob).where(ValidationJob.id == world["job_id"]).values(status="failed")
    )
    await db_session.commit()
    body = (await client.get(handoff_url(world), headers=world["admin"])).json()
    assert body["state"] == "unavailable" and body["solutions"] == []


async def test_handoff_expired_after_scrub(client, db_session, world):
    await finish(db_session, world)
    # The purge deletes the raw result and marks the report scrubbed.
    await db_session.execute(QaResult.__table__.delete().where(QaResult.job_id == world["job_id"]))
    await db_session.execute(
        update(Report)
        .where(Report.id == world["report"].id)
        .values(description="", scrubbed_at=datetime.now(UTC))
    )
    await db_session.commit()
    response = await client.get(handoff_url(world), headers=world["dev"])
    assert response.status_code == 200
    body = response.json()
    assert (body["state"], body["job_id"], body["verdict"]) == (
        "expired",
        world["job_id"],
        "reproduced",
    )
    assert body["summary"] is None and body["solutions"] == []


async def test_handoff_stranger_404(client, db_session, world):
    await finish(db_session, world)
    assert (await client.get(handoff_url(world), headers=world["stranger"])).status_code == 404
    assert (await client.get(handoff_url(world), headers=world["other_admin"])).status_code == 404
    assert (await client.get(handoff_url(world))).status_code == 401


async def test_job_artifacts_listed_for_org_only(client, db_session, world):
    shot = await write_artifact(
        db_session, world, "image_" + "a" * 64 + ".png", png(), "screenshot"
    )
    await write_artifact(
        db_session,
        world,
        "old.json",
        b"{}",
        "json",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    url = f"/api/v1/validation-jobs/{world['job_id']}/artifacts"
    response = await client.get(url, headers=world["dev"])
    assert response.status_code == 200
    [item] = response.json()["items"]
    assert set(item) == {"id", "kind", "size", "created_at"}
    assert (item["id"], item["kind"]) == (shot, "screenshot")
    assert "storage" not in response.text
    assert (await client.get(url, headers=world["stranger"])).status_code == 404


async def test_download_headers_and_audit(client, db_session, world):
    artifact_id = await write_artifact(db_session, world, "result.json", b'{"a": 1}', "json")
    response = await client.get(download_url(artifact_id), headers=world["dev"])
    assert response.status_code == 200
    assert response.content == b'{"a": 1}'
    assert response.headers["content-type"] == "application/json"
    assert response.headers["content-disposition"] == f'attachment; filename="{artifact_id}.json"'
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "sandbox; default-src 'none'"
    assert response.headers["cache-control"] == "private, no-store"
    events = (
        await db_session.scalars(select(AuditEvent).where(AuditEvent.action == "evidence.download"))
    ).all()
    assert [(e.target, e.detail) for e in events] == [(artifact_id, None)]


async def test_content_type_fixed_by_kind_not_file_name(client, db_session, world):
    # A .html file name must not change the served type.
    artifact_id = await write_artifact(db_session, world, "evil.html", b"<script>", "screenshot")
    response = await client.get(download_url(artifact_id), headers=world["dev"])
    assert response.headers["content-type"] == "image/png"


async def test_download_other_org_404(client, db_session, world):
    artifact_id = await write_artifact(db_session, world, "result.json", b"{}", "json")
    for who in ("other_admin", "stranger"):
        assert (await client.get(download_url(artifact_id), headers=world[who])).status_code == 404
    assert (await client.get(download_url("0" * 32), headers=world["dev"])).status_code == 404


async def test_anonymous_evidence_401(client, db_session, world):
    artifact_id = world["report"].input_artifact_id
    assert (await client.get(download_url(artifact_id))).status_code == 401


async def test_input_image_download_org_only(client, db_session, world):
    input_id = world["report"].input_artifact_id
    own = await client.get(download_url(input_id), headers=world["dev"])
    assert own.status_code == 200
    assert own.headers["content-type"] == "image/png"
    assert own.content.startswith(b"\x89PNG")
    assert (await client.get(download_url(input_id), headers=world["stranger"])).status_code == 404


async def test_expired_artifact_404(client, db_session, world):
    artifact_id = await write_artifact(
        db_session,
        world,
        "x.json",
        b"{}",
        "json",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert (await client.get(download_url(artifact_id), headers=world["dev"])).status_code == 404


async def test_report_md_links_rewritten_to_evidence_urls(client, db_session, world):
    name = "image_" + "b" * 64 + ".png"
    shot = await write_artifact(db_session, world, name, png(), "screenshot")
    md = f"# 보고서\n\n![검증 화면]({name})\n\n![없는 화면](image_{'c' * 64}.png)\n".encode()
    md_id = await write_artifact(db_session, world, "report.md", md, "report_md")
    response = await client.get(download_url(md_id), headers=world["dev"])
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    text = response.text
    assert f"![검증 화면](/api/v1/evidence/{shot}/download)" in text
    assert f"](image_{'c' * 64}.png)" in text  # no matching artifact: left untouched


@pytest.mark.parametrize("key", ["../../etc/passwd", "/etc/passwd", "a/../../outside.json"])
async def test_storage_key_traversal_rejected(client, db_session, world, tmp_path, key):
    (tmp_path / "outside.json").write_text("{}")
    artifact_id = new_id()
    db_session.add(
        Artifact(
            id=artifact_id,
            org_id=world["report"].org_id,
            report_id=world["report"].id,
            job_id=world["job_id"],
            kind="json",
            storage_key=key,
            sha256="0" * 64,
            size=2,
            visibility="org",
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    await db_session.commit()
    assert (await client.get(download_url(artifact_id), headers=world["dev"])).status_code == 404


async def test_symlink_escaping_root_rejected(client, db_session, world, tmp_path):
    secret = tmp_path / "secret.json"
    secret.write_text('{"secret": true}')
    artifact_id = await write_artifact(db_session, world, "link.json", b"{}", "json")
    path = storage.storage_root() / (await db_session.get(Artifact, artifact_id)).storage_key
    path.unlink()
    path.symlink_to(secret)
    assert (await client.get(download_url(artifact_id), headers=world["dev"])).status_code == 404
