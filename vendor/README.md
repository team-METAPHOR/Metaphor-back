# vendor

`metaphor_core_engine-0.4.0-py3-none-any.whl` is built from the private repository
`team-METAPHOR/Core_engine` at commit `e396348cdd28c3b5326c8fd4da0e126ddf262b99`
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
| Version | `0.0.0` (`package.json`; pre-release build, no GitHub Release yet) |
| Source | `team-METAPHOR/Metaphor-sdk` main @ `d5a7131b1f483ef72b1db109cb6ebb5d73d3b8d3` (`npm ci && npm run build` → `dist/metaphor.js`) |
| SRI | `sha384-/jWCOMS5CXN8OyQGKQpFE74LnL+rzwZvGacXX5rE7FFNwgQSq09WnopIKDD6dBR2` |

`sdk/VERSION` holds the version on line 1 and the SDK commit on line 2. The version is the
pinned URL (`metaphor-<version>.js`, cached as `immutable`), so **never change the bundle
without bumping the version**.

To re-vendor from a GitHub Release asset:

```bash
scripts/vendor-sdk.sh v0.1.0      # downloads metaphor.js, writes VERSION, prints the SRI
# update the table above, then:
git add -f vendor/sdk && uv run pytest -q tests/test_sdk_serving.py
```
