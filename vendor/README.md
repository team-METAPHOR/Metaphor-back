# vendor

`metaphor_core_engine-0.5.0-py3-none-any.whl` is built from the private repository
`team-METAPHOR/Core_engine` at commit `093535decc7deb8f3dfb61a994880d30f09e41d0`
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
| Version | `0.1.1` |
| Source | GitHub Release `v0.1.1` of `team-METAPHOR/Metaphor-sdk` @ `adeebea6d6a46bd77db7c0031ea4d24733083be4` (`scripts/vendor-sdk.sh v0.1.1`) |
| SRI | `sha384-40tPvdAPO7apKv4/QsIttvbVK4EYjfJhclZ+kNlhblxv/0IIFgduaY280rWd1blb` (matches the release's `metaphor.js.sha384`) |

`sdk/VERSION` holds the version on line 1 and the SDK commit on line 2. The version is the
pinned URL (`metaphor-<version>.js`, cached as `immutable`), so **never change the bundle
without bumping the version**.

To re-vendor from a GitHub Release asset:

```bash
scripts/vendor-sdk.sh v0.1.1      # downloads metaphor.js, writes VERSION, prints the SRI
# update the table above, then:
git add -f vendor/sdk && uv run pytest -q tests/test_sdk_serving.py
```
