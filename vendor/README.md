# vendor

`metaphor_core_engine-0.2.0-py3-none-any.whl` is built from the private repository
`team-METAPHOR/Core_engine` at commit `d04c2328a59c4cb18dc79989aea56615304c2352`
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
