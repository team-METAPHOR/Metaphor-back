from app.core.config import Settings


def test_allowed_origins_trailing_slash_and_space_are_ignored(monkeypatch):
    # Browsers send Origin without a path, so "http://host:3000/" would never match.
    monkeypatch.setenv(
        "ALLOWED_ORIGINS", '["http://localhost:3000/", " https://app.example.com// "]'
    )
    assert Settings(_env_file=None).allowed_origins == [
        "http://localhost:3000",
        "https://app.example.com",
    ]
