"""Open a program's sealed test account in the worker (the only holder of the private key).

ponytail: core_engine 4a710f0 can only send fixed `BrowserOptions.session_headers`; it cannot
log in with an id/password yet, so runs do not call this. Once the engine has a login step,
`build_service` (app/worker/runner.py) passes `load_test_account(...)` there, the API's
`TestAccountResponse.in_use` flips to true, and `TEST_ACCOUNT_*` codes below join
`report_actions.CODE_ACTIONS` → TEST_ACCOUNT_REQUIRED.
"""

import os

import psycopg

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
