"""Approved test writes: stored revision value ↔ core_engine 0.6.0 `StateChangePolicy`.

Shared by the API (validation) and the worker (run policy). The only source is the admin-set
program revision, frozen into the job snapshot; never report text, SDK data or model output.
"""

from core_engine import StateChangePolicy


def state_change_policy(value: dict | None) -> StateChangePolicy | None:
    if not value:
        return None
    return StateChangePolicy(
        environment=value["environment"],
        reset_adapter_id=value["reset_adapter_id"],
        allowed_requests=tuple((method, path) for method, path in value["allowed_requests"]),
    )
