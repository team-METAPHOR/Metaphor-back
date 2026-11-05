"""Test credentials (session headers, test account login) only go to HTTPS targets.

core_engine 0.4.1 refuses a credential-bearing run on a non-HTTPS base URL with
`CREDENTIALS_REQUIRE_HTTPS`, with no localhost/private-network exception. The API rejects the
combination when it is configured, and the worker checks again right before a run.
"""

from urllib.parse import urlsplit

from app.core.errors import ApiError

CODE = "CREDENTIALS_REQUIRE_HTTPS"
MESSAGE = (
    "테스트 계정이나 세션을 사용하려면 프로그램 원점이 HTTPS여야 합니다. "
    "인증 없는 HTTP 검증은 가능합니다."
)


def is_https(base_url: str) -> bool:
    return urlsplit(base_url).scheme.lower() == "https"


def require_https(base_url: str) -> None:
    """Raise the stable 422 when credentials would be used against `base_url`."""
    if not is_https(base_url):
        raise ApiError(422, CODE, MESSAGE)
