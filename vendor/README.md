# vendor

`metaphor_core_engine-0.8.9-py3-none-any.whl` is built from the private repository
`team-METAPHOR/Core_engine` at commit `eeaa5ea672a59ead7bf5171141641d8bde74962a`
(`uv build --wheel`). It is vendored so CI and new checkouts install the engine without
a cross-repository access token.

To upgrade the engine:

```bash
gh repo clone team-METAPHOR/Core_engine /tmp/ce && git -C /tmp/ce checkout <commit>
uv build --wheel -o vendor /tmp/ce
# update the file name in pyproject.toml [tool.uv.sources] if the version changed, then:
uv lock && uv sync --all-groups && uv run pytest -q
```

Record the new commit hash here and in AGENTS.md.

## Web SDK bundle (`sdk/`)

`sdk/metaphor.js` is the Metaphor Web SDK bundle served at `GET /sdk/v1/metaphor.js` and
`GET /sdk/v1/metaphor-<version>.js` (`app/api/sdk.py`). The API reads it once at startup and
computes the SRI hash from the bytes, so the hash below is for reference only.

| Field | Value |
|---|---|
| Version | `0.2.0` |
| Source | `team-METAPHOR/Metaphor-sdk` @ `2058fc606642443f0d6316b53e0e68a45dea2ea1` (GitHub Release `v0.2.0` asset) |
| SRI | `sha384-SOhEfhQwr68acxK5/YDPTNcpFnINajiQKMx9tDR0SQPv2dsMWSmDHucj8DpcnZT1` (matches the release `metaphor.js.sha384`) |

`sdk/VERSION` holds the version on line 1 and the SDK commit on line 2. The version is the
pinned URL (`metaphor-<version>.js`, cached as `immutable`), so **never change the bundle
without bumping the version**.

To re-vendor from a GitHub Release asset:

```bash
scripts/vendor-sdk.sh v0.2.0      # downloads metaphor.js, writes VERSION, prints the SRI
# update the table above, then:
git add -f vendor/sdk && uv run pytest -q tests/test_sdk_serving.py
```

`versions/0.1.1/` and `versions/0.1.2/` preserve the previous immutable bundles and metadata. The API loads archived
versions at startup, so existing pinned install snippets keep their original bytes and SRI.
SDK 0.2.0 activity context needs engine 0.8.1 or later (`reporter_activity`).
