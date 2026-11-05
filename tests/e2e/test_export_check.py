"""The exported Playwright test (#53): stored bytes equal the engine's, and the same spec fails
on the buggy build and passes on the fixed one.

Offline (scripted model) through the real API, worker and engine. Running the spec needs Node
and `@playwright/test`: `npm ci --prefix tests/e2e/js` (same version as the Python Playwright,
so it reuses the installed Chromium). Without them only the hash/environment part runs.
"""

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from app.models import QaResult
from tests.e2e.test_acceptance import (  # noqa: F401  (fixtures)
    _worker,
    get_json,
    run_worker,
    screenshot,
    site,  # noqa: F811
    world,  # noqa: F811
)
from tests.fakes import ScriptedModel, analysis, intake, plan

pytestmark = pytest.mark.browser

JS = Path(__file__).parent / "js"
CLI = JS / "node_modules/@playwright/test/cli.js"
NO_PLAYWRIGHT_JS = (
    None
    if shutil.which("node") and CLI.is_file()
    else "node or @playwright/test missing: run `npm ci --prefix tests/e2e/js`"
)


def run_spec(spec: bytes, env: dict[str, str]) -> subprocess.CompletedProcess:
    # Playwright only finds specs under its cwd (the js/ project), like the engine's own test.
    with tempfile.TemporaryDirectory(prefix="export-", dir=JS) as temporary:
        return _run(spec, env, Path(temporary))


def _run(spec: bytes, env: dict[str, str], tmp: Path) -> subprocess.CompletedProcess:
    (tmp / "reproduction.spec.ts").write_bytes(spec)
    (tmp / "reset.mjs").write_text("export async function reset() { return true; }\n")
    clean = {k: v for k, v in os.environ.items() if not k.startswith("QA_")}
    clean.update(env, QA_RESET_MODULE=str(tmp / "reset.mjs"))
    return subprocess.run(
        ["node", str(CLI), "test", str(tmp / "reproduction.spec.ts"), "--workers=1",
         "--reporter=line", f"--output={tmp / 'results'}"],
        cwd=JS, env=clean, capture_output=True, text=True, timeout=120,
    )  # fmt: skip


async def test_exported_spec_fails_on_bug_and_passes_when_fixed(
    client,
    db_session,
    world,  # noqa: F811
    site,  # noqa: F811
    conn,  # noqa: F811
):
    _, state = site
    url = f"{world['base']}/app/search"
    created = await client.post(
        "/api/v1/reports",
        data={
            "program_id": world["program"].id,
            "target_url": url,
            "description": "cat 검색 후 missing으로 검색하면 cat 목록이 그대로 남아요",
        },
        files={"screenshot": ("s.png", screenshot(), "image/png")},
        headers=world["dev_h"],
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["validation_job_id"]
    await run_worker(conn, job_id, ScriptedModel([intake(), plan(url), analysis()]))

    raw = (await db_session.get(QaResult, job_id)).raw
    exported = raw["report"]["test_export"]
    assert exported["status"] == "ready", exported
    artifacts = await get_json(
        client, f"/api/v1/validation-jobs/{job_id}/artifacts", world["admin_h"]
    )
    spec_row = next(a for a in artifacts["items"] if a["kind"] == "playwright_spec")
    response = await client.get(
        f"/api/v1/evidence/{spec_row['id']}/download", headers=world["admin_h"]
    )
    assert response.status_code == 200
    spec = response.content
    # Byte-identical to what the engine hashed (persist also refuses a mismatching file).
    assert hashlib.sha256(spec).hexdigest() == exported["artifact"]["sha256"]
    assert len(spec) == exported["artifact"]["bytes"]
    required = exported["required_environment"]
    assert "QA_RESET_MODULE" in required  # trusted fixture reset, supplied by the runner
    assert not {"QA_AUTHORIZATION", "QA_COOKIE"} & set(required)  # no session for this plan

    if NO_PLAYWRIGHT_JS:
        pytest.skip(NO_PLAYWRIGHT_JS)
    env = {"QA_ENTRY_URL": url}  # optional: defaults to the plan's own first target
    buggy = run_spec(spec, env)
    assert buggy.returncode != 0, buggy.stdout[-2000:] + buggy.stderr[-1000:]
    assert "1 failed" in buggy.stdout, buggy.stdout[-2000:]  # the assertion, not a setup error
    state["fixed"] = True
    fixed = run_spec(spec, env)
    assert fixed.returncode == 0, fixed.stdout[-2000:] + fixed.stderr[-1000:]
    assert "1 passed" in fixed.stdout, fixed.stdout[-2000:]
