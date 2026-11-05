# vendor

`metaphor_core_engine-0.8.0-py3-none-any.whl` is built from the private repository
`team-METAPHOR/Core_engine` at commit `4c6b024e861b96c76203038b43c2c20ba965d882`
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
| Source | `team-METAPHOR/Metaphor-sdk` @ `1dd5718ba190258c4d4b1d27010b435cf40115da` (`npm run build`; release pending) |
| SRI | `sha384-Otqm+J0vaiAui09ne5QDRi6Jarpxd89Mh8RvdaZqB2dKcFW8V6PzIV30+e/d5Mo1` (computed from the local build) |

`sdk/VERSION` holds the version on line 1 and the SDK commit on line 2. The version is the
pinned URL (`metaphor-<version>.js`, cached as `immutable`), so **never change the bundle
without bumping the version**.

To re-vendor from a GitHub Release asset:

```bash
scripts/vendor-sdk.sh v0.2.0      # downloads metaphor.js, writes VERSION, prints the SRI
# update the table above, then:
git add -f vendor/sdk && uv run pytest -q tests/test_sdk_serving.py
```

`versions/0.1.1/` preserves the previous immutable bundle and metadata. The API loads archived
versions at startup, so existing pinned install snippets keep their original bytes and SRI.
New 0.2.0 code and engine 0.8.0 must be reviewed before deployment; no release tag is created here.
