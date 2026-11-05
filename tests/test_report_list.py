"""GET /reports filters + page mode, org report-summary and report-stats."""

from datetime import UTC, datetime, timedelta
from itertools import product

import pytest
from sqlalchemy import String, literal, select, union_all

from app.core.ids import new_id
from app.models import ValidationJob
from app.models.report import WORKFLOW_STATUSES
from app.models.validation_job import JOB_STATUSES, VERDICTS
from app.schemas.validation_job import JobBrief
from app.services.reports import PROGRESS_VALUES, progress_case, report_progress
from tests.factories import login_as, make_org, make_program, make_report

NOW = datetime.now(UTC)


@pytest.fixture
async def world(client, db_session):
    org = await make_org(db_session)
    program = await make_program(db_session, org)
    await db_session.commit()
    admin, admin_h = await login_as(client, db_session, org, "admin")
    _, dev_h = await login_as(client, db_session, org, "developer")
    return {"org": org, "program": program, "admin": admin, "admin_h": admin_h, "dev_h": dev_h}


async def seed(db_session, world, *, workflow="received", job=None, **fields):
    """A report (created `age` ago) and optionally its latest job given as a dict."""
    age = fields.pop("age", timedelta(0))
    report = await make_report(
        db_session,
        world["org"],
        world["program"],
        world["admin"],
        workflow_status=workflow,
        created_at=NOW - age,
        **fields,
    )
    if job is not None:
        await add_job(db_session, report, **job)
    await db_session.commit()
    return report


async def add_job(db_session, report, *, kind="initial", status="completed", verdict=None, **kw):
    job = ValidationJob(
        id=new_id(),
        report_id=report.id,
        org_id=report.org_id,
        kind=kind,
        status=status,
        verdict=verdict,
        scope_revision=1,
        scope_snapshot={},
        **kw,
    )
    db_session.add(job)
    await db_session.flush()
    report.latest_job_id = job.id
    await db_session.flush()
    return job


async def get(client, world, query="", headers=None):
    response = await client.get(f"/api/v1/reports{query}", headers=headers or world["dev_h"])
    assert response.status_code == 200, response.text
    return response.json()


def ids(body):
    return [item["id"] for item in body["items"]]


# --- progress: SQL CASE mirrors the Python mapping ---------------------------


async def test_progress_case_agrees_with_python_for_every_case(db_session):
    cases = [(w, None, None) for w in WORKFLOW_STATUSES]
    cases += list(product(WORKFLOW_STATUSES, JOB_STATUSES, (*VERDICTS, None)))
    rows = [
        select(
            literal(i),
            progress_case(
                literal(w, String),
                literal(None if s is None else "j", String),
                literal(s, String),
                literal(v, String),
            ),
        )
        for i, (w, s, v) in enumerate(cases)
    ]
    sql = dict((await db_session.execute(union_all(*rows))).all())
    for i, (w, s, v) in enumerate(cases):
        job = None if s is None else JobBrief(id="j", kind="initial", status=s, verdict=v)
        expected = report_progress(w, job)
        assert expected in PROGRESS_VALUES
        assert sql[i] == expected, (w, s, v)


# --- GET /reports filters --------------------------------------------------


async def test_progress_filter_repeatable_and_unknown_values_rejected(client, db_session, world):
    new = await seed(db_session, world)
    reproducing = await seed(db_session, world, job={"status": "running"})
    reproduced = await seed(db_session, world, job={"verdict": "reproduced"})
    await seed(db_session, world, job={"verdict": "not_reproduced"})
    body = await get(client, world, "?progress=new&progress=reproduced")
    assert set(ids(body)) == {new.id, reproduced.id}
    assert ids(await get(client, world, "?progress=reproducing")) == [reproducing.id]
    # Values another branch introduces are accepted and simply match nothing yet.
    assert ids(await get(client, world, "?progress=verified&progress=waiting_quota")) == []
    bad = await client.get("/api/v1/reports?progress=bogus", headers=world["dev_h"])
    assert (bad.status_code, bad.json()["error"]["code"]) == (422, "VALIDATION_FAILED")


async def test_closed_hidden_unless_asked(client, db_session, world):
    open_ = await seed(db_session, world)
    closed = await seed(db_session, world, workflow="closed", closed_at=NOW)
    assert ids(await get(client, world)) == [open_.id]
    assert set(ids(await get(client, world, "?include_closed=true"))) == {open_.id, closed.id}
    assert ids(await get(client, world, "?progress=closed")) == [closed.id]
    assert ids(await get(client, world, "?progress=new")) == [open_.id]


async def test_created_range_from_inclusive_to_exclusive(client, db_session, world):
    old = await seed(db_session, world, age=timedelta(days=10))
    mid = await seed(db_session, world, age=timedelta(days=5))
    await seed(db_session, world)
    start = (NOW - timedelta(days=6)).isoformat()
    end = (NOW - timedelta(days=1)).isoformat()
    params = {"from": start, "to": end}
    response = await client.get("/api/v1/reports", params=params, headers=world["dev_h"])
    assert ids(response.json()) == [mid.id]
    exact = {"from": old.created_at.isoformat(), "to": mid.created_at.isoformat()}
    response = await client.get("/api/v1/reports", params=exact, headers=world["dev_h"])
    assert ids(response.json()) == [old.id]
    swapped = await client.get(
        "/api/v1/reports", params={"from": end, "to": start}, headers=world["dev_h"]
    )
    assert (swapped.status_code, swapped.json()["error"]["code"]) == (422, "VALIDATION_FAILED")


async def test_q_searches_title_and_description_case_insensitive(client, db_session, world):
    by_title = await seed(db_session, world, title="Checkout Button broken")
    by_desc = await seed(db_session, world, description="the CHECKOUT page hangs")
    await seed(db_session, world, title="login", description="nothing")
    percent = await seed(db_session, world, title="100% off_coupon")
    found = await client.get("/api/v1/reports", params={"q": "checkout"}, headers=world["dev_h"])
    assert set(ids(found.json())) == {by_title.id, by_desc.id}
    # LIKE wildcards are literal characters.
    for q in ("%", "_", "0% o", "f_c"):
        response = await client.get("/api/v1/reports", params={"q": q}, headers=world["dev_h"])
        assert ids(response.json()) == [percent.id], q
    too_long = await client.get("/api/v1/reports", params={"q": "x" * 101}, headers=world["dev_h"])
    assert too_long.status_code == 422


async def test_filters_combine_with_source_program_assignee(client, db_session, world):
    other_program = await make_program(db_session, world["org"])
    hit = await seed(db_session, world, title="pay", assignee_id=world["admin"].id)
    await seed(db_session, world, title="pay")
    await make_report(db_session, world["org"], other_program, world["admin"], title="pay")
    await db_session.commit()
    query = f"?q=pay&program_id={world['program'].id}&assignee={world['admin'].id}&source=platform"
    assert ids(await get(client, world, query)) == [hit.id]


# --- page-number pagination ------------------------------------------------


async def test_page_mode_returns_total_and_page(client, db_session, world):
    reports = [await seed(db_session, world, age=timedelta(minutes=i)) for i in range(5)]
    await seed(db_session, world, workflow="closed")
    first = await get(client, world, "?limit=2")
    assert (first["total"], first["page"]) == (5, 1)
    assert ids(first) == [reports[0].id, reports[1].id] and first["next_cursor"]
    third = await get(client, world, "?limit=2&page=3")
    assert (third["total"], third["page"], ids(third)) == (5, 3, [reports[4].id])
    assert third["next_cursor"] is None
    beyond = await get(client, world, "?limit=2&page=9")
    assert (beyond["total"], beyond["items"]) == (5, [])
    assert (await get(client, world, "?limit=2&include_closed=true"))["total"] == 6
    assert (await get(client, world, "?limit=2&progress=new&include_closed=true"))["total"] == 5


async def test_cursor_mode_unchanged_and_not_combined_with_page(client, db_session, world):
    for i in range(3):
        await seed(db_session, world, age=timedelta(minutes=i))
    first = await get(client, world, "?limit=2")
    rest = await get(client, world, f"?limit=2&cursor={first['next_cursor']}")
    assert len(rest["items"]) == 1 and rest["next_cursor"] is None
    assert (rest["total"], rest["page"]) == (None, None)
    both = await client.get(
        f"/api/v1/reports?page=2&cursor={first['next_cursor']}", headers=world["dev_h"]
    )
    assert (both.status_code, both.json()["error"]["code"]) == (422, "VALIDATION_FAILED")
    zero = await client.get("/api/v1/reports?page=0", headers=world["dev_h"])
    assert zero.status_code == 422


async def test_list_is_members_only(client, db_session, world):
    await seed(db_session, world)
    _, stranger_h = await login_as(client, db_session)
    body = await get(client, world, "", stranger_h)
    assert (body["items"], body["total"]) == ([], 0)


# --- GET /orgs/{org_id}/report-summary ---------------------------------------


async def summary(client, world, headers=None):
    return await client.get(
        f"/api/v1/orgs/{world['org'].id}/report-summary", headers=headers or world["dev_h"]
    )


async def test_summary_counts_by_progress_and_needs_attention(client, db_session, world):
    await seed(db_session, world)
    await seed(db_session, world, job={"status": "queued"})
    older = await seed(
        db_session, world, title="old", job={"verdict": "reproduced"}, age=timedelta(hours=2)
    )
    needs_info = await seed(
        db_session, world, title="q", job={"status": "needs_info"}, age=timedelta(hours=1)
    )
    newest = await seed(db_session, world, title="new", job={"verdict": "reproduced"})
    await seed(db_session, world, job={"verdict": "not_reproduced"})
    await seed(db_session, world, workflow="closed")
    # needs_info on a report already being fixed needs no attention.
    await seed(db_session, world, workflow="fixing", job={"status": "needs_info"})
    response = await summary(client, world)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body["counts"]) == set(PROGRESS_VALUES)
    assert {k: v for k, v in body["counts"].items() if v} == {
        "new": 1,
        "reproducing": 1,
        "reproduced": 2,
        "triaged": 2,
        "fixing": 1,
        "closed": 1,
    }
    assert body["needs_attention"] == [
        {"id": r.id, "number": r.number, "title": r.title, "progress": p}
        for r, p in ((newest, "reproduced"), (needs_info, "triaged"), (older, "reproduced"))
    ]


async def test_summary_needs_attention_top_five(client, db_session, world):
    for i in range(7):
        await seed(db_session, world, job={"verdict": "reproduced"}, age=timedelta(minutes=i))
    body = (await summary(client, world)).json()
    assert len(body["needs_attention"]) == 5


async def test_summary_and_stats_members_only(client, db_session, world):
    other = await make_org(db_session, "Other")
    await db_session.commit()
    _, other_h = await login_as(client, db_session, other, "admin")
    for path in ("report-summary", "report-stats"):
        response = await client.get(f"/api/v1/orgs/{world['org'].id}/{path}", headers=other_h)
        assert (response.status_code, response.json()["error"]["code"]) == (404, "NOT_FOUND")
        missing = await client.get(f"/api/v1/orgs/{'f' * 32}/{path}", headers=world["dev_h"])
        assert missing.status_code == 404
        anonymous = await client.get(f"/api/v1/orgs/{world['org'].id}/{path}")
        assert anonymous.status_code == 401


# --- GET /orgs/{org_id}/report-stats ------------------------------------------


async def stats(client, world, query=""):
    response = await client.get(
        f"/api/v1/orgs/{world['org'].id}/report-stats{query}", headers=world["dev_h"]
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_stats_empty_org_nulls(client, world):
    assert await stats(client, world) == {
        "days": 7,
        "auto_reproduction_rate": None,
        "avg_reproduction_seconds": None,
        "fix_verified_count": 0,
        "by_page": [],
    }


async def test_stats_rates_durations_retests_and_pages(client, db_session, world):
    start = NOW - timedelta(hours=1)

    async def report_with(initial, *, url="https://staging.example.com/cart?x=1", **fields):
        report = await seed(db_session, world, target_url=url, **fields)
        job = await add_job(db_session, report, created_at=start, **initial)
        await db_session.commit()
        return report, job

    # 1: reproduced on the initial run, took 60 s.
    await report_with({"verdict": "reproduced", "finished_at": start + timedelta(seconds=60)})
    # 2: initial not reproduced (120 s), a later fresh run reproduced it.
    second, _ = await report_with(
        {"verdict": "not_reproduced", "finished_at": start + timedelta(seconds=120)}
    )
    await add_job(
        db_session, second, kind="fresh", verdict="reproduced", finished_at=NOW, created_at=NOW
    )
    # 3: inconclusive (180 s); 4: still running (not counted); 5: cancelled (not counted).
    await report_with(
        {"status": "inconclusive", "finished_at": start + timedelta(seconds=180)},
        url="https://staging.example.com/login",
        client_context={"reported_url": "https://shop.example.com/checkout/pay?card=1"},
    )
    await report_with({"status": "running"}, url="https://staging.example.com/login")
    await report_with({"status": "cancelled", "finished_at": NOW})
    # Retests: one passed (fix verified), one reproduced again.
    third, _ = await report_with({"verdict": "reproduced", "finished_at": NOW})
    for verdict in ("not_reproduced", "reproduced"):
        await add_job(db_session, third, kind="retest", verdict=verdict, finished_at=NOW)
    # Outside the window: ignored everywhere.
    old = await seed(db_session, world, age=timedelta(days=30), target_url="https://x.test/old")
    await add_job(
        db_session,
        old,
        verdict="reproduced",
        created_at=NOW - timedelta(days=30),
        finished_at=NOW - timedelta(days=30),
    )
    await db_session.commit()

    body = await stats(client, world)
    # finished initial jobs: 1, 2, 3, third(4 total) -> reproduced: 1, 2 (via fresh), third.
    assert body["auto_reproduction_rate"] == pytest.approx(3 / 4)
    finished_seconds = [60, 120, 180, (NOW - start).total_seconds()]
    assert body["avg_reproduction_seconds"] == pytest.approx(sum(finished_seconds) / 4, abs=0.01)
    assert body["fix_verified_count"] == 1
    assert body["by_page"] == [
        {"path": "/cart", "count": 4},
        {"path": "/checkout/pay", "count": 1},
        {"path": "/login", "count": 1},
    ]


async def test_stats_days_clamped_and_by_page_top_ten(client, db_session, world):
    for i in range(12):
        for _ in range(12 - i):
            await seed(db_session, world, target_url=f"https://staging.example.com/p{i:02d}")
    body = await stats(client, world, "?days=0")
    assert body["days"] == 1
    assert [p["path"] for p in body["by_page"]] == [f"/p{i:02d}" for i in range(10)]
    assert (await stats(client, world, "?days=1000"))["days"] == 90


async def test_naive_range_is_utc(client, db_session, world):
    report = await seed(db_session, world, age=timedelta(hours=3))
    naive = (NOW - timedelta(hours=4)).replace(tzinfo=None).isoformat()
    response = await client.get("/api/v1/reports", params={"from": naive}, headers=world["dev_h"])
    assert ids(response.json()) == [report.id]
    later = (NOW - timedelta(hours=2)).replace(tzinfo=None).isoformat()
    response = await client.get("/api/v1/reports", params={"from": later}, headers=world["dev_h"])
    assert ids(response.json()) == []
