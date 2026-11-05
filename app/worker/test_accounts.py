"""Open a program's sealed test account in the worker (the only holder of the private key).

`load_login` turns it into core_engine 0.4.0 `LoginOptions`, which `build_service` and
`conditions.build_conditions` (app/worker/) pass to every run whose context signs in
(`models.test_account.login_applies`). The credentials live only in memory: the engine
keeps them out of the model, plan, result and evidence, and we never log or store them.
"""

import os

import psycopg
from core_engine import LoginOptions

from app.core import test_account_crypto as crypto
from app.worker.runner import JobInputError

PRIVATE_KEY_ENV = "TEST_ACCOUNT_PRIVATE_KEY"


def private_key():
    pem = os.environ.get(PRIVATE_KEY_ENV, "")
    if not pem.strip():
        raise JobInputError("TEST_ACCOUNT_KEY_MISSING")
    try:
        return crypto.load_private_key(pem)
    except crypto.AccountKeyError:
        raise JobInputError("TEST_ACCOUNT_KEY_INVALID") from None


def load_test_account(conn: psycopg.Connection, program_id: str) -> crypto.Credentials | None:
    """The program's current account, or None when none is stored.

    The row's own program id and version are the associated data, so a row moved to another
    program, replayed under another version, or sealed for another key fails with
    TEST_ACCOUNT_INVALID. The plaintext never leaves the returned object (its repr is redacted).
    """
    row = conn.execute(
        "SELECT version, key_id, wrapped_key, ciphertext FROM program_test_accounts"
        " WHERE program_id = %s",
        (program_id,),
    ).fetchone()
    if row is None:
        return None
    version, key_id, wrapped_key, ciphertext = row
    sealed = crypto.Sealed(key_id, bytes(wrapped_key), bytes(ciphertext))
    try:
        return crypto.open_sealed(private_key(), program_id, version, sealed)
    except crypto.AccountDecryptError:
        raise JobInputError("TEST_ACCOUNT_INVALID") from None


def load_login(conn: psycopg.Connection, program_id: str) -> LoginOptions | None:
    """Engine login for the program's account, or None when none is stored or it lacks a
    login path / success signal (runs then stay anonymous, and the API's `in_use` is false)."""
    row = conn.execute(
        "SELECT login_path, submit_path, success_path, success_selector"
        " FROM program_test_accounts WHERE program_id = %s",
        (program_id,),
    ).fetchone()
    if row is None or not row[0] or not (row[2] or row[3]):
        return None
    credentials = load_test_account(conn, program_id)
    if credentials is None:
        return None  # deleted in between
    login_path, submit_path, success_path, success_selector = row
    try:
        return LoginOptions(
            login_path=login_path,
            login_id=credentials.login_id,
            password=credentials.password,
            submit_path=submit_path,
            success_path=success_path,
            success_selector=success_selector,
        )
    except ValueError:
        # e.g. a credential embedded in the stored paths; the message is never surfaced.
        raise JobInputError("TEST_ACCOUNT_INVALID") from None
