"""Generate the test-account keypair.

Run: `uv run python -m app.worker.keys [DIR]` → DIR/test_account_private.pem (0600, worker
`TEST_ACCOUNT_PRIVATE_KEY` only) and DIR/test_account_public.pem (API
`TEST_ACCOUNT_PUBLIC_KEY`). Existing files are never overwritten. Prints only the key id.
"""

import os
import sys
from pathlib import Path

from app.core import test_account_crypto as crypto


def write_keypair(directory: Path) -> str:
    private_pem, public_pem = crypto.generate_keypair()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name, pem, mode in (
        ("test_account_private.pem", private_pem, 0o600),
        ("test_account_public.pem", public_pem, 0o644),
    ):
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w") as file:
            file.write(pem)
    return crypto.key_id(crypto.load_public_key(public_pem))


def main(argv: list[str]) -> None:
    directory = Path(argv[0]) if argv else Path(".")
    try:
        print(f"key_id={write_keypair(directory)} dir={directory.resolve()}")
    except FileExistsError as error:
        sys.exit(f"refusing to overwrite {error.filename}")


if __name__ == "__main__":
    main(sys.argv[1:])
