"""core_engine 0.6.0 result fields survive storage and reach the read APIs (#102):
`environment.state_changes`, `run.evidence[].blocked_requests`, `state_changing_requests`,
`observations[].network[].non_fatal`, `diagnosis.next_actions` (incl. condition children)."""

from app.core.text import redact_tree
from app.services import condition_results, diagnosis
from app.services.report_actions import derive_actions
from app.worker.persist import normalize
from tests.test_handoff_diagnosis import STEPS, engine_report, evidence, finish_with, get
from tests.test_handoff_evidence import world  # noqa: F401

APPROVAL = {
    "environment": "test",
    "reset_adapter_id": "buglab-v1",
    "allowed_requests": [["PATCH", "/api/cart/items"], ["POST", "/api/cart/items"]],
}
BLOCKED = {
    "url": "https://cdn.vendor.example/sdk.js?key=pk_live_123#x",
    "method": "GET",
    "resource_type": "script",
    "blocker": "ORIGIN_OUT_OF_SCOPE",
    "send_attempted": False,
    "non_fatal": True,
}
WRITES = [
    {
        "method": "POST",
        "path": "/api/cart/items",
        "send_attempted": True,
        "status": 201,
        "blocker": None,
    },
    {
        "method": "PATCH",
        "path": "/api/cart/items/2",
        "send_attempted": True,
        "status": 200,
        "blocker": None,
    },
    {
        "method": "DELETE",
        "path": "/api/cart/items/2",
        "send_attempted": False,
        "status": None,
        "blocker": "STATE_CHANGE_OUT_OF_SCOPE",
    },
]
NETWORK = [
    {
        "url": "https://staging.example.com/api/auth/me",
        "method": "GET",
        "resource_type": "fetch",
        "status": 401,
        "blocker": None,
        "send_attempted": True,
        "non_fatal": True,
    },
    {**BLOCKED, "status": None},
]


def report_06():
    items = []
    for attempt in (1, 2):
        item = evidence(attempt, len(STEPS), symptom=True, expected=False)
        item.update(blocked_requests=[BLOCKED], state_changing_requests=WRITES)
        items.append(item)
    verdict = {"value": "reproduced", "reason_code": "SYMPTOM_OBSERVED_TWICE"}
    return engine_report(
        verdict=verdict,
        evidence_items=items,
        network=NETWORK,
        environment={"width": 1280, "height": 800, "state_changes": APPROVAL},
    )


def test_persisted_result_keeps_new_fields():
    """The worker stores the whole (redacted) engine result: no 0.6.0 key is dropped."""
    report = report_06()
    stored = redact_tree(normalize({"status": "completed", "report": report}))["report"]
    assert stored["environment"]["state_changes"] == APPROVAL
    assert stored["run"]["evidence"][0]["blocked_requests"][0]["non_fatal"] is True
    assert stored["run"]["evidence"][1]["state_changing_requests"] == WRITES
    assert stored["observations"][0]["network"][0]["non_fatal"] is True
    codes = [a["code"] for a in stored["diagnosis"]["next_actions"]]
    assert "REVIEW_BLOCKED_SUBRESOURCES" in codes


async def test_handoff_diagnosis_exposes_engine_06_fields(client, db_session, world):  # noqa: F811
    await finish_with(db_session, world, report_06(), verdict="reproduced")
    body = (await get(client, world)).json()
    result = body["diagnosis"]
    assert result["state_changes"] == APPROVAL
    first = result["attempts"][0]
    assert first["blocked_requests"] == [
        {
            "method": "GET",
            # Query and fragment never leave: they can carry keys.
            "url": "https://cdn.vendor.example/sdk.js",
            "resource_type": "script",
            "blocker": "ORIGIN_OUT_OF_SCOPE",
            "send_attempted": False,
            "non_fatal": True,
        }
    ]
    assert first["state_changing_requests"] == WRITES
    facts = {(f["url"], f["status"]): f for f in result["observed_facts"]}
    assert facts[("https://staging.example.com/api/auth/me", 401)]["non_fatal"] is True
    assert facts[("https://cdn.vendor.example/sdk.js", None)]["non_fatal"] is True
    actions = {a["code"]: a for a in result["next_actions"]}
    assert actions["REVIEW_BLOCKED_SUBRESOURCES"]["owner"] == "developer"
    assert "pk_live_123" not in str(body)


async def test_read_only_result_has_empty_lists(client, db_session, world):  # noqa: F811
    verdict = {"value": "reproduced", "reason_code": "SYMPTOM_OBSERVED_TWICE"}
    items = [evidence(1, 3, symptom=True, expected=False), evidence(2, 3, symptom=True)]
    await finish_with(db_session, world, engine_report(verdict=verdict, evidence_items=items))
    result = (await get(client, world)).json()["diagnosis"]
    assert result["state_changes"] is None
    assert all(a["blocked_requests"] == [] for a in result["attempts"])
    assert all(a["state_changing_requests"] == [] for a in result["attempts"])


def test_diagnosis_drops_malformed_items():
    report = report_06()
    report["run"]["evidence"][0]["blocked_requests"] = [
        {"url": "javascript:alert(1)", "method": "GET", "blocker": "ORIGIN_OUT_OF_SCOPE"},
        "not a dict",
        {**BLOCKED, "blocker": "lower_case", "extra": "/srv/secret"},
    ]
    report["run"]["evidence"][0]["state_changing_requests"] = [
        {"method": "POST", "path": "/api/cart?coupon=SECRET", "status": 200},
        {"method": "<script>", "path": "/x"},
    ]
    report["environment"]["state_changes"] = {"environment": "production"}
    _, built = diagnosis.build("completed", {"report": report})
    attempt = built.attempts[0]
    assert [b.url for b in attempt.blocked_requests] == ["https://cdn.vendor.example/sdk.js"]
    assert attempt.blocked_requests[0].blocker is None
    assert [(w.method, w.path) for w in attempt.state_changing_requests] == [("POST", "/api/cart")]
    assert built.state_changes is None


def test_condition_rows_keep_child_diagnosis():
    """Condition children: the engine copies each child's diagnosis/environment into its row."""
    child = report_06()
    row = {
        "condition_id": "desktop",
        "status": "completed",
        "verdict": "reproduced",
        "diagnosis": child["diagnosis"],
        "environment": child["environment"],
    }
    built = condition_results.row_diagnosis(row)
    assert built.state_changes is not None
    assert built.state_changes.reset_adapter_id == "buglab-v1"
    assert "REVIEW_BLOCKED_SUBRESOURCES" in [a.code for a in built.next_actions]
    assert built.outcome == "reproduced"
    assert condition_results.row_diagnosis({"condition_id": "x", "status": "not_run"}) is None


def test_reset_and_write_codes_map_to_required_actions():
    class Job:
        status = "inconclusive"
        backend_error_code = None
        error_code = "RESET_FAILED"

    assert [a.code for a in derive_actions("new", Job, None)] == ["CHECK_TEST_RESET"]
    Job.error_code, Job.backend_error_code = None, "RESET_ACCOUNT_NOT_APPROVED"
    assert [a.code for a in derive_actions("new", Job, None)] == ["CHECK_TEST_RESET"]
    Job.backend_error_code = None
    raw = {"report": {"run": {"evidence": [{"blocker": "METHOD_NOT_ALLOWED"}]}}}
    assert [a.code for a in derive_actions("new", Job, raw)] == ["REVIEW_WRITE_APPROVAL"]
