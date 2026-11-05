# AGENTS.md — Metaphor Backend

Guide for coding agents (Claude Code, Codex, etc.) and humans working in this repository.
Read this file fully before changing code.

## 1. Service overview

Metaphor is a screenshot-driven web QA service.

- A bug report for a customer's web app is a screenshot, a text description, or both. Reports
  arrive from the **Metaphor Web SDK** installed on a customer site (anonymous, identified by a
  public project key) or from customer org members filing them manually in the Metaphor dashboard.
  There are no external reporters: logged-in users outside the owner org cannot submit or read.
- The backend stores the report and enqueues a **validation job**.
- A separate Python **worker** runs `metaphor-core-engine` (`QAService`), which interprets the
  report with an AI model, plans steps, reproduces the bug twice in fresh Playwright browser
  contexts inside the customer's **approved test scope**, and writes a verdict
  (`reproduced` / `not_reproduced` / `inconclusive`), evidence, and a Korean report.
- Customer org members read results; developers and admins answer follow-up questions
  (`resume`) and re-verify a fix with the same stored plan (`retest`).

Out of scope: editing customer source code, applying patches, opening PRs, email/Slack
notifications, external issue creation.

## 2. Glossary

| Term | Meaning |
|---|---|
| Organization (org) | Customer company that owns programs and sees results |
| Program | One customer web app under test. `id` doubles as engine `program_id` |
| Scope revision | Immutable snapshot of approved origin, path prefixes, viewport, test context, masks |
| Report | A bug submission. `source` = `platform` (org member, `author_id` set) or `widget` |
| Validation job | One engine execution. `kind` = `initial` / `fresh` / `resume` / `retest` |
| Engine job id | 32-hex id created by the engine's `JobStore`; mapped to our job id, never exposed |
| Artifact | Stored file (input image, report.md, screenshots, JSON) owned by an org |
| Widget settings | Per-program Web SDK config: public project key, allowed origins, toggles |

## 3. Architecture

```text
HTTP request
   ↓
API / Router      request parsing, response mapping, dependencies (auth, CSRF)
   ↓
Service           permissions, business rules, state transitions, transactions
   ↓
Repository        queries and persistence (no commit)
   ↓
Model             SQLAlchemy ORM on PostgreSQL

Schema            Pydantic request/response DTOs
Core              settings, errors, logging, security helpers, ids
Integration       engine adapters and storage helpers
Worker            separate process: claims jobs from PostgreSQL and runs the engine
```

```text
app/
├── main.py                FastAPI app, middleware, router registration
├── api/
│   ├── dependencies.py    DbSession, CurrentUser, CSRF guard
│   └── v1/
│       ├── router.py      /api/v1 router
│       └── endpoints/     one module per feature (auth, programs, reports, widget, ...)
├── services/              business logic, one module per feature
├── repositories/          data access, one module per aggregate
├── models/                ORM models (import every module in models/__init__.py)
├── schemas/               DTOs
├── core/                  config, errors, logging, security, ids
├── db/                    async engine and session
├── integrations/          engine/storage adapters
└── worker/                job queue loop, engine runner, purge
migrations/                Alembic
tests/                     pytest suites (tests/e2e needs a real model key)
```

### Layer rules

- API never writes SQL and never touches ORM objects in responses — map to Schemas.
- Services do not depend on FastAPI `Request`. Pass plain values (user, client IP hash, origin).
- Transactions live in services: `async with session.begin():`. Repositories only `flush()`.
- Same feature name across layers: `endpoints/reports.py`, `services/reports.py`,
  `repositories/reports.py`, `models/report.py`, `schemas/report.py`.
- The API process never imports Playwright or runs `QAService`. It may import
  `core_engine.Screenshot`, `core_engine.RunPolicy`, `core_engine.StateChangePolicy`,
  `core_engine.canonical_url`, and `core_engine.redaction.redact_text`.
- The queue is the `validation_jobs` table (`FOR UPDATE SKIP LOCKED`). Do not add a broker.

## 4. Invariants (do not break)

- Job status: `queued → running → (needs_info | completed | inconclusive | failed | cancelled)`,
  plus `cancelling`. A terminal job never returns to `running`.
- At most one active job (`queued`/`running`/`cancelling`) per report — enforced by a partial
  unique index, not only app code.
- `reports.workflow_status` (human review) is separate from job status and AI classification.
  The system never changes it: a passing retest only sets `fix_verified`, and a person closes
  the report (`closed` is allowed only while `fix_verified` is true).
- Every job creation goes through the shared `create_job` service (daily limit, active-job check).
- Every report creation goes through the shared `create_report` service (platform and widget).
- Cross-org access returns **404**, not 403, so existence is not leaked.
- Programs, platform report submission, reports, jobs and evidence are for active members of
  the owner org only. Roles then gate writes: `admin` (org, members, invites, programs/scope,
  widget, origin verification) ⊃ `developer` (file reports, rerun/retest, answer, cancel,
  workflow, assignee) ⊃ `viewer` (read only; every write is 403). Role tuples live only in
  `app/services/access.py` (`READ_ROLES`, `WRITE_ROLES`, `ADMIN_ROLES`).
- Anonymous widget reporters can never read results, jobs, evidence, or other reports.
- Engine internals (`_baseline`, `_parent`) are never called directly. Use `run/resume/retest`.

## 5. Security rules

- Secrets (`OPENAI_API_KEY`, `GROQ_API_KEY`, `QA_SESSION_<REF>` test session headers,
  `TEST_ACCOUNT_PRIVATE_KEY`) exist only in the worker environment. Never in API settings,
  responses, logs, queue rows, or tests' snapshots.
- Program test accounts (login id + password) are the one customer secret stored in the DB, and
  only sealed (`app/core/test_account_crypto.py`: AES-256-GCM + RSA-OAEP, bound to program id,
  version and key id). The API holds only `TEST_ACCOUNT_PUBLIC_KEY` and can seal but never
  open; only the worker decrypts. Never return, log or audit the password, full login id,
  ciphertext or keys; validation errors never echo input.
- Uploaded screenshots: validate with `core_engine.Screenshot`, apply reporter `mask_boxes`
  with `Screenshot.masked`, persist **only** the masked image. Never keep the raw upload.
- Store report text only after `redact_text`. Length limits apply to the raw input.
- Never collect request/response bodies, cookies, tokens, or auth headers from the Web SDK.
- Logs pass through the redacting filter. Do not log request bodies or query strings.
- Model output is untrusted data: return as plain text, never render as HTML, never follow URLs.
- Storage paths are built only from server-generated ids; resolve and check they stay under
  `STORAGE_ROOT`. Directories 0700, files 0600.
- The public project key is an identifier, not a credential. Origin checks are a browser-side
  guard, not authentication; rate limits and scope checks are the real controls.

## 6. Commands

```bash
cp .env.example .env
uv sync --all-groups
docker compose up -d db
uv run alembic upgrade head
uv run uvicorn app.main:app --reload        # API
uv run python -m app.worker.main            # worker (needs OPENAI_API_KEY + Chromium)
uv run pytest -q                            # unit/integration tests
uv run pytest -m e2e                        # real engine + model, skipped without key
uv run ruff check . && uv run ruff format --check .
```

Schema changes: edit models → `uv run alembic revision --autogenerate -m "<message>"` →
review the generated file → `uv run alembic upgrade head`. Never create tables at app startup.

## 7. Git workflow

- `main` is protected in practice: never commit directly to it.
- One task = one branch = one PR.
- **Stacked PRs.** Roadmap tasks form one linear stack. Each branch is created from the
  previous task's branch, and its PR targets that branch (the bottom PR targets `main`):

  ```text
  main ← docs/agents-md ← chore/test-harness ← feat/core-errors-logging ← ...
  ```

  ```bash
  git switch feat/core-errors-logging          # previous task in the stack
  git switch -c feat/core-auth-session         # new task branch
  # ... work, commit ...
  git push -u origin feat/core-auth-session
  gh pr create --base feat/core-errors-logging --title "[core] 세션 인증과 CSRF 방어 추가"
  ```

  - Keep each PR reviewable on its own: only that task's diff.
  - When a lower branch changes, update the ones above it:
    `git rebase --update-refs <lower-branch>` from the top branch, then
    `git push --force-with-lease` for each updated branch.
  - Merge bottom-up. After a PR merges, retarget the next PR to `main`.
- **PR title:** `[scope] 한국어 요약` (Korean summary, no trailing period).
  Scopes: `global` (repo-wide docs/config/tooling), `core` (shared logic), `program`,
  `report`, `worker`, `widget`, `evidence`, `test`.
- Branch names: `<type>/<short-kebab-topic>`

  | Prefix | Use |
  |---|---|
  | `feat/` | new behavior (`feat/core-intake`, `feat/widget-public-api`) |
  | `fix/` | bug fix |
  | `refactor/` | behavior-preserving change |
  | `test/` | tests only |
  | `docs/` | documentation only |
  | `chore/` | tooling, deps, config |

- Commits follow Conventional Commits: `feat: ...`, `fix: ...`, `chore: ...`, `test: ...`.
  Subject in English, imperative, ≤ 72 chars.
- Before pushing: `ruff check`, `ruff format --check`, `pytest -q` all pass.
- A PR description lists: what changed, migration included (yes/no), tests added, and the
  PR below it in the stack.

## 8. Working rules for agents

- Follow the implementation roadmap kept outside the repo
  (`../2026-11-05-implementation-roadmap.md`). Common tasks (C*) come before feature tasks.
- TDD: write the failing test first, see it fail, implement, see it pass, commit.
- Prefer the smallest change that satisfies the task. Reuse shared services (`create_report`,
  `create_job`, `run_once`, `enforce_rate_limit`, `record_audit`) instead of re-implementing.
- No new dependency without a reason stated in the PR. Stdlib first.
- Do not modify the engine (`metaphor-core-engine`, pinned at `48ec7bb`), Playwright behavior,
  or model prompts from this repo.
- Error responses use the shared envelope:
  `{"error": {"code": "UPPER_SNAKE", "message": "...", "request_id": "..."}}`.
- Times are UTC ISO 8601. Ids are `uuid4().hex`.
