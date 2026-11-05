import logging
from contextlib import nullcontext
from threading import Event
from unittest.mock import Mock

import pytest

from app.worker import main, startup


@pytest.fixture(autouse=True)
def model_name(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.4-mini")


def test_missing_model_key_stops_before_database_or_browser(monkeypatch, caplog):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    database = Mock()
    monkeypatch.setattr(startup.psycopg, "connect", database)
    with pytest.raises(startup.WorkerStartupError, match="WORKER_MODEL_KEY_MISSING"):
        startup.check_startup()
    database.assert_not_called()
    assert "WORKER_MODEL_KEY_MISSING" in caplog.text


@pytest.mark.parametrize("failure", ["database", "browser"])
def test_startup_failure_logs_only_stable_code(monkeypatch, caplog, failure):
    secret = "synthetic-secret-that-must-never-be-logged"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    connection = Mock()
    connect = Mock(return_value=nullcontext(connection))
    browser_check = Mock()
    if failure == "database":
        connect.side_effect = RuntimeError(secret)
        expected = "WORKER_DATABASE_UNAVAILABLE"
    else:
        browser_check.side_effect = RuntimeError(secret)
        expected = "WORKER_CHROMIUM_UNAVAILABLE"
    monkeypatch.setattr(startup.psycopg, "connect", connect)
    monkeypatch.setattr(startup, "check_chromium", browser_check)
    with pytest.raises(startup.WorkerStartupError, match=expected):
        startup.check_startup()
    assert expected in caplog.text
    assert secret not in caplog.text
    if failure == "database":
        browser_check.assert_not_called()


def test_startup_checks_database_browser_and_video(monkeypatch, caplog):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-key")
    connection = Mock()
    monkeypatch.setattr(startup.psycopg, "connect", Mock(return_value=nullcontext(connection)))
    browser_check = Mock()
    video_check = Mock(return_value=True)
    monkeypatch.setattr(startup, "check_chromium", browser_check)
    monkeypatch.setattr(startup, "check_video_encoder", video_check)
    with caplog.at_level(logging.INFO):
        startup.check_startup()
    connection.execute.assert_called_once_with("SELECT 1")
    browser_check.assert_called_once_with()
    video_check.assert_called_once_with()
    assert "WORKER_STARTUP_OK" in caplog.text


def test_shutdown_finishes_current_job_without_claiming_another(monkeypatch):
    stop = Event()
    monkeypatch.setattr(main.psycopg, "connect", Mock(return_value=nullcontext(Mock())))
    monkeypatch.setattr(main.queue, "reap_expired", Mock())
    claim = Mock(return_value="job-one")
    monkeypatch.setattr(main.queue, "claim_next", claim)

    def finish(*args, **kwargs):
        stop.set()

    supervise = Mock(side_effect=finish)
    monkeypatch.setattr(main, "supervise", supervise)
    main.run_forever(Mock(), stop_event=stop)
    claim.assert_called_once()
    supervise.assert_called_once()


def test_shutdown_while_idle_does_not_claim_again(monkeypatch):
    stop = Event()
    monkeypatch.setattr(main.psycopg, "connect", Mock(return_value=nullcontext(Mock())))
    monkeypatch.setattr(main.queue, "reap_expired", Mock())

    def idle(*args, **kwargs):
        stop.set()
        return None

    claim = Mock(side_effect=idle)
    monkeypatch.setattr(main.queue, "claim_next", claim)
    main.run_forever(Mock(), stop_event=stop)
    claim.assert_called_once()


def test_worker_entrypoint_exposes_lifecycle_logs(monkeypatch, caplog):
    monkeypatch.setattr(startup, "check_startup", Mock())
    monkeypatch.setattr(main, "run_forever", Mock())
    monkeypatch.setattr(main.signal, "signal", Mock())
    assert main.main() == 0
    assert "WORKER_STOPPED" in caplog.text
