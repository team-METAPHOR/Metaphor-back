# vendor

`metaphor_core_engine-0.3.0-py3-none-any.whl` is built from the private repository
`team-METAPHOR/Core_engine` at commit `4a710f088063c258cef3664a890f715be10faad2`
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
