import pytest

from app.core.config import get_settings
from tests.eval import bug_site
from tests.eval.harness import EvalEnv
from tests.factories import login_as, make_org


@pytest.fixture
def site():
    with bug_site.serve() as value:
        yield value


@pytest.fixture
async def eval_env(client, db_session, conn, site, worker_env, monkeypatch):
    """Same world as tests/e2e: real API + worker against a loopback-only synthetic target."""
    monkeypatch.setattr(get_settings(), "ip_hash_secret", "eval-secret")
    org = await make_org(db_session)
    _, admin_h = await login_as(client, db_session, org, "admin")
    return EvalEnv(client, db_session, conn, site, org, admin_h)
