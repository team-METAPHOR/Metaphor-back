"""Test-data reset adapters, registered by id (program revision `reset_adapter`).

An adapter factory receives the job's scope snapshot and returns the async `reset(attempt)` the
engine calls before the first observation (0) and before each of the two runs (1, 2). It must
return exactly True only after the synthetic account/data is back in its known state.
"""

from collections.abc import Awaitable, Callable

Reset = Callable[[int], Awaitable[bool]]


def _stateless(snapshot: dict) -> Reset:
    # Only for targets without server-side state (demo pages). Never use for stateful apps.
    async def reset(attempt: int) -> bool:
        return True

    return reset


RESETS: dict[str, Callable[[dict], Reset]] = {"stateless": _stateless}
