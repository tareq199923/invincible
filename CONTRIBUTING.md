# Contributing to Invincible

Thanks for helping. This page covers everything needed to build, test, and
land a change. Read it once; after that the linked references go deeper.

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
Security problems must **not** go in public issues — see
[SECURITY.md](SECURITY.md).

---

## Ways to help

- **Report a bug** with the [bug report template](.github/ISSUE_TEMPLATE/bug_report.md).
- **Request a feature** with the [feature request template](.github/ISSUE_TEMPLATE/feature_request.md).
- **Improve the docs.** A wrong command or stale screenshot is a real bug.
- **Send code.** Provider compatibility, failover edge cases, and dashboard
  polish are always welcome. Small, focused PRs land fastest.

If you are new, look for issues labelled `good first issue`. If you want to
work on something larger, open an issue first so we can agree on scope before
you spend time on it.

---

## Development setup

**Requirements:** Python 3.10+ (CI covers 3.10–3.14) and PostgreSQL (CI and
the test suite run PostgreSQL 17).

```bash
git clone https://github.com/tareq199923/invincible.git
cd invincible
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -e ".[dev]"
```

Most of the test suite needs a real PostgreSQL instance. There is no SQLite
fallback. Provision one locally:

```bash
invincible dev-db            # starts/verifies Postgres and prints INVINCIBLE_DB_URL
# or: docker compose up db
```

Then point the server at it and create the schema:

```bash
invincible setup --db-url postgresql://invincible@127.0.0.1:5433/invincible
invincible db upgrade
```

Migrations are **never** applied automatically — run `invincible db upgrade`
after pulling changes that touch the schema.

### Tests

The suite expects this database by default:

```text
postgresql+asyncpg://invincible@127.0.0.1:5433/invincible_test
```

Override it with `INVINCIBLE_TEST_DATABASE_URL`. Upstream providers are always
faked with `httpx.MockTransport`; no provider API keys are needed.

```bash
ruff check .            # lint gate — must be clean
pytest                  # full suite
pytest tests/test_router.py            # one file
pytest tests/test_router.py::test_name # one test
pytest -m slow          # packaging smoke test (builds a wheel in a scratch venv)
```

`pytest.ini` defaults to `-m "not slow"`, so the wheel test only runs when you
ask for it. CI runs it in the release workflow.

Details, fixtures, and the per-file coverage map:
[docs/TESTING.md](docs/TESTING.md).

---

## Project layout

| Path | What lives there |
|---|---|
| `invincible/main.py` | FastAPI app, lifespan, auth dependencies, `/health` |
| `invincible/endpoints/` | HTTP surfaces: OpenAI/Anthropic/Responses compat, MCP, accounts, dashboard, docs |
| `invincible/core/` | Business logic: router, stores, continuity, memory, tool executor, settings |
| `invincible/compat/` | Protocol translators (OpenAI, Anthropic, Responses) |
| `invincible/agent/` | The local agent that runs on the user's machine |
| `invincible/migrations/` | Packaged Alembic environment (`invincible db upgrade`) |
| `tests/` | pytest suite |
| `docs/` | Deep reference documentation |

Full module map and request flows: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Ground rules

These are load-bearing. A PR that breaks one of them will be sent back even if
the tests pass.

1. **One failover loop.** `core/router.py::_iter_attempts` is the only attempt
   loop. `route_request` / `stream_open` are thin wrappers. Never write a
   second loop or duplicate the failover classification.
2. **Schema truth lives in `core/db.py`.** Any table change updates the
   metadata *and* adds a packaged Alembic revision under
   `invincible/migrations/`, verified against `create_all`.
3. **Env reads go through `core/settings.py`.** `cli.py` is the documented
   exception.
4. **Layering.** The compat layer never imports the Router. Route handlers stay
   thin. Stores are thin repositories. Business logic lives in `core/`.
5. **Auth realms stay separate.** `/v1/*` uses per-user `inv_` API keys;
   account surfaces use signed session cookies; `/mcp` uses OAuth bearer
   tokens. Do not merge them or flip fail-open/fail-closed semantics.
6. **Secrets are never logged or echoed.** DSNs are password-masked in output.
   Provider credentials are referenced by env-var name and resolved at call
   time.
7. **Wire compatibility is tested behavior.** SSE event order, tool-call
   round-trips, and error mapping are pinned by tests. Change them only
   deliberately, by updating those tests.
8. **Every feature ships with tests**, and `ruff check .` must pass.

`AGENTS.md` contains the same rules written for AI coding assistants; it is
the guide to hand to an agent working in this repo.

### Maintainer notebook

`PROGRESS.md`, `feature_list.json`, `templates/clean-state-checklist.md`, and
`docs/HARNESS_PLAN.md` are the maintainers' working notebook. They are
committed on purpose, but outside contributors do not need to edit them.
`python tools/audit_harness.py` checks that notebook and runs in CI.

---

## Pull requests

1. Fork, branch from `main`, and keep the change focused. One concern per PR.
2. Match the existing style. Commit messages use a short prefix:
   `fix:`, `feat:`, `docs:`, `test:`, `chore:`, `build(deps):`.
3. Update the docs your change affects. Docs follow implementation — never
   document a feature before its code and tests land.
4. Fill in the PR template, including the security question.
5. Make sure `ruff check .` and `pytest` are clean.

A maintainer will review, ask questions, and merge. Keep the PR title
descriptive — it may become the commit message when the branch is squashed.

### AI-assisted contributions

Welcome. State that the change was AI-assisted in the PR description, and make
sure you understand and can defend every line. The repo's invariants still
apply; "the model wrote it" is not a reason to skip tests.

---

## Documentation map

| Change touches | Update |
|---|---|
| Env vars, CLI flags | `docs/CONFIGURATION.md`, `.env.example` |
| Endpoints, request/response shapes | `docs/API_REFERENCE.md`, `README.md` |
| Provider schema | `docs/PROVIDERS.md` |
| MCP tools or protocol | `docs/MCP_PROTOCOL.md`, `invincible/endpoints/docs.py` |
| Auth, denylists, approvals | `docs/SECURITY.md` |
| Deployment requirements | `docs/DEPLOYMENT.md` |
| Architecture or module layout | `docs/ARCHITECTURE.md`, `README.md` |

The public docs site is rendered from `invincible/endpoints/docs.py`, while
`docs/*.md` is the deep reference. If you change user-facing behavior, check
both.

---

## License

Contributions are accepted under the [MIT License](LICENSE).
