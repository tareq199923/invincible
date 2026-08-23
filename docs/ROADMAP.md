# Roadmap — Invincible (ai-gateway)

The planned future of the project, organized into phases. Each phase is a
self-contained unit of work with a goal, the problem it solves, concrete
scope (mapped to real files where useful), dependencies, acceptance
criteria, and a rough size.

---

## How to read this roadmap

- **Priority** — `P1` (do first), `P2` (important), `P3` (when the
  foundation is in place).
- **Size** — `S` (days), `M` (1–2 weeks), `L` (multi-week, likely broken
  into smaller PRs).
- **Status** — `Planned` / `In progress` / `Done`. Phases 0, 6, 9, 10,
  and 11 are done; the rest are planned.
- **Dependencies** — phases listed as prerequisites must land first; the
  arrows matter more than the numbers.

```
Phase 0 (current state)
   │
   ├─► Phase 11 Repo hygiene ─► Phase 12 Fixes ─► Phase 14 CI ─► Phase 13 Refactor + Settings ─► Phase 16 PostgreSQL storage ─┐
   │                                                                                                                          │
   ├─► Phase 2  Zero-clone distribution ◄─────────────────────────────────────────────────────────────────────────────────────┘
   │         │                                                  (PyPI launches after the DB story settles)
   │         └─► Phase 3  Documentation site
   │
   ├─► Phase 1  Security hardening ◄─ audit table designed in 16 ─► Phase 4  Multi-user system ◄── 16 (real storage)
   │                                                                  │
   ├─► Phase 15 Observability ───────────────────────────────────► Phase 5  Dashboard
   │
   ├─► Phase 7  More MCP tools            (independent)
   ├─► Phase 8  Deployment                (compose app+postgres pair lands in 16)
   │
   Done: Phase 6 More providers · Phase 9 Context compression · Phase 10 Context memory · Phase 11 Repo hygiene
```

The intended execution order across all three tracks (engineering quality,
product, platform):

```
Foundation    11 → 12 → 14      hygiene, bug fixes, CI guardrails first
Refactor      13                protected by green CI; Settings owns DB config
Storage       16                PostgreSQL-only migration; biggest single phase
Product       2 → 3             PyPI after 16: one install story, no SQLite era
Platform      1 → 4             audit surface on the 16 tables, then users
Observe       15 → 5            metrics land just before their consumer
Then          7 · 8             parallel finishers
```

---

## Phase 0 — Current state (Done)

What exists today, so each phase's delta is clear.

| Area | State |
|---|---|
| **Chat gateway** | OpenAI-compatible `POST /v1/chat/completions` + Anthropic `POST /v1/messages`, both streaming (SSE), translated through one internal message model (`invincible/compat/`). |
| **Failover** | Tiered routing in `core/router.py` + `core/provider_health.py`: 429/5xx → exponential cooldown (30s→300s), 401/403 → permanent disable, other 4xx → verbatim forward, all down → 503. Cooldowns are in-memory only. |
| **Conversation memory** | SQLite `core/session_store.py`, keyed by `X-Session-Id`, with per-provider context trimming. Stored plaintext. |
| **MCP tool server** | `POST /mcp` (`initialize`, `tools/list`, `tools/call`) with `read_file` (ungated), `execute_bash` / `write_file` (token-approval gated), `confirm_action`. Denylists are text-pattern matches. |
| **Auth** | `GATEWAY_API_KEY` bearer for `/v1/*` (plain `==` compare); OAuth 2.1 + PKCE for `/mcp` (`core/oauth_store.py`, `endpoints/oauth.py`) with browser owner login, per-client consent, short-lived access tokens, hashed tokens at rest. |
| **CLI** | `invincible`/`inv`: `setup`, `start` (auto Cloudflare tunnel), `secret rotate`, `doctor`, `oauth list/revoke/test-client`. |
| **Packaging** | `pyproject.toml`, package name `invincible-ai`, console scripts declared, `providers.yaml` packaged. Not yet on PyPI. |
| **Docs** | 8 docs in `docs/` (architecture, API, config, MCP, security, testing, providers, roadmap). No changelog. |
| **Tests** | 17 test files, pytest + pytest-asyncio, fake upstreams via `httpx.MockTransport`, no live providers ever called. |

---

## Phase 1 — Security hardening

**Priority P1 · Size M · Status: Planned · Prerequisite for: Phase 4**

### Goal

Close the gaps documented in [SECURITY.md](SECURITY.md) §7 known limits,
starting with the two that block multi-user: **no audit trail** and **no
per-approver identity** on the MCP approval flow.

### Problem it solves

- Today *anyone* holding a live OAuth token can approve `execute_bash` /
  `write_file` — and there is no record of who approved what, when.
  That is both an accountability hole and a blocker for multi-user.
- `GATEWAY_API_KEY` is compared with plain `==` (`main.py` `require_auth`),
  not a timing-safe digest.
- Owner-login rate limiting is per-IP and in-memory; a restart clears it.
- Sessions and grants persist plaintext in `sessions.db`.

### Scope

1. **MCP approval audit log** (the core of this phase)
   - The audit **table schema is designed and created in Phase 16** as part
     of the PostgreSQL migration; this phase populates it and builds the
     surface on top:
     `id`, `timestamp`, `client_id`, `action_type`, `command`/`path` (the
     sensitive args), `token`, `decision` (staged / approved / declined /
     expired), and `approver_client_id`.
   - Written from `core/tool_executor.py` (`execute_bash`, `write_file`,
     `confirm_action`) and surfaced with a new CLI surface, e.g.
     `invincible audit list [--client <id>] [--since …]` — no raw SQL.
   - `confirm_action` resolves the *approver* from the OAuth bearer token
     used on the `/mcp` request (`endpoints/mcp.py` → pass identity into
     `confirm_action`).
   - Persistence: piggyback on `INVINCIBLE_PERSIST_PENDING_ACTIONS` or
     always-on — decide and document; audit entries should survive restarts.
   - Tests: approve/deny/expiry each leave one audit row with the right
     approver; unknown token leaves no row.
2. **Timing-safe `GATEWAY_API_KEY` compare**
   - Swap plain `==` for `hmac.compare_digest` in `main.py` `require_auth`
     (mirrors what the owner secret already does).
   - *(Lands early via Phase 12; kept here as the acceptance gate.)*
3. **Persist login rate-limit state** (or document the tradeoff)
   - Move the per-IP failure window from in-memory to the database so a
     restart does not reset brute-force friction (`endpoints/oauth.py`).
   - *(Lands with Phase 16, which migrates all state to PostgreSQL and
     persists provider health in the same pass.)*
4. **Optional sessions-at-rest encryption**
   - Toggle (env var) that encrypts conversation rows in
     `core/session_store.py`; default off to preserve the existing
     plaintext behavior and zero-migration startup.
5. **README/SECURITY doc refresh** — the known-limits list shrinks to what
   is actually true after this phase.

### Dependencies

- Phase 0 (everything hooks into existing stores).

### Acceptance criteria

- Every `confirm_action` decision is recorded with approver identity and is
  reviewable via the CLI without SQL.
- `GATEWAY_API_KEY` comparison is timing-safe.
- Rate-limit state survives a server restart.
- Full test suite stays green; new tests cover 1–3.
- SECURITY.md §7 updated to reflect the changes.

---

## Phase 2 — Zero-clone distribution

**Priority P1 · Size M · Status: Planned · Prerequisite for: Phase 3**

### Goal

A user should **never clone this repo**. The entire onboarding is:

```bash
pipx install invincible-ai
invincible setup
invincible start
```

### Problem it solves

Right now the README says "clone the repo, create a venv, install
requirements" — that is a contributor flow, not a product flow. The product
promise is: one install command, one interactive setup, done.

### Scope

1. **Publish to PyPI**
   - Verify `pyproject.toml` metadata (name `invincible-ai`, description,
     license, classifiers, `providers.yaml` package-data already set).
   - CI job to build + publish on tags (`build` is already a dev dep).
   - `pipx install invincible-ai` must work on Windows/macOS/Linux; verify
     the `invincible` / `inv` console scripts.
   - **Ordering: this phase launches after Phase 16.** The product is
     PostgreSQL-only by decision; publishing before that migration would
     strand early users on a storage engine the project has abandoned.
     The install story is therefore "pipx + a Postgres URL or the bundled
     docker compose pair", not zero-config — say so plainly in every
     onboarding surface.
2. **`invincible setup` as the single entry point** (`cli.py`)
   - Already generates missing secrets and prompts for provider keys —
     audit the flow from a *fresh machine* perspective: what would a
     non-developer find confusing? Remove any step that assumes the repo
     was cloned (e.g. paths, docs links).
   - Add a friendly success banner with "next steps" (start the server,
     open the tunnel, first curl).
3. **Version check / upgrade**
   - `invincible doctor` reports the installed version and the latest
     published one.
   - `invincible upgrade` (or documented `pipx upgrade invincible-ai`)
     path in the README.
4. **First-run experience**
   - `invincible setup` on a machine with no `.env` at all must end with a
     working server (`invincible start`), including the Cloudflare tunnel
     path being optional but discoverable.

### Dependencies

- Phase 0.

### Acceptance criteria

- `pipx install invincible-ai` on a clean machine (no repo) → `invincible
  setup` → `invincible start` → health check 200.
- A released tag produces a PyPI artifact automatically.
- `invincible doctor` shows version + latest published version.

---

## Phase 3 — Documentation site

**Priority P2 · Size M · Status: Planned · Prerequisite: none (supports Phase 2 onboarding)**

### Goal

A public website replacing "read the README" as the entry point for
installing, configuring, and using Invincible.

### Problem it solves

All knowledge lives in `docs/*.md` inside the repo — invisible to a user
who never clones it. Phase 2 makes installs clone-free; the docs must
follow.

### Scope

1. **Static site generator** — MkDocs (Material theme, matches Python
   ecosystem and markdown source) or Vitepress. Recommend MkDocs: the
   existing `docs/*.md` files are the content already.
2. **Site sections**
   - **Docs** — architecture, API reference, MCP protocol (the existing
     files, lightly reorganized for a site).
   - **Configuration** — every `.env` variable and `providers.yaml` field,
     with a copy-paste template.
   - **Instructions** — install → setup → start → tunnel, per platform
     (Windows/macOS/Linux).
   - **Providers** — tier table, how to get each key, how to add a
     provider (from `providers.yaml` + README's tier table).
   - **Connections** — Claude Code, OpenAI clients, cloud AI over
     tunnel: one config snippet each.
   - **Updates** — generated changelog page (link to GitHub releases) so
     users see what changed.
   - **FAQ** — from the README's known limits + common issues.
3. **Changelog discipline** — introduce `CHANGELOG.md` (keep a Changes
   section per release) and surface it on the site.
4. **Deploy** — GitHub Pages from a `docs-site/` build, or the deployment
   story of Phase 8 once it exists.

### Dependencies

- None technically; ordered after Phase 2 because clone-free onboarding
  makes the site necessary.

### Acceptance criteria

- The site is live with the six sections above.
- Every README "known limit" is represented somewhere on the site.
- A new provider key can be added by a user following only the site.

---

## Phase 4 — Multi-user system

**Priority P1 · Size L · Status: Planned · Requires: Phase 1**

### Goal

More than one person (or agent) can use one gateway: separate identities,
separate sessions, separate quotas — with an owner who administers it.

### Problem it solves

Today `X-Session-Id` is a partition key, not an identity; anyone with
`GATEWAY_API_KEY` shares one session namespace and one usage pool. Multi-user
turns the gateway into a shared service.

### Scope

1. **User accounts & auth on `/v1/*`**
   - User registry (PostgreSQL, on the Phase 16 store): id, display name,
     hashed password or
     per-user API key, role (`owner` / `user`).
   - Per-user keys verified timing-safely (Phase 1); `/v1/*` auth becomes
     "which user is this".
2. **Per-user sessions**
   - Session namespace scoped to the authenticated user:
     `core/session_store.py` keys become `(user_id, session_id)`.
   - Migration path for the existing `default` session.
3. **MCP approver identity**
   - OAuth grants already carry `client_id`; attach the owning user to each
     client in `core/oauth_store.py` so the Phase 1 audit log records a
     *user*, not just a client id.
4. **Quotas & usage**
   - Per-user token/request accounting (usage already computed in
     `compat/common.py` `build_usage`); store per-user totals; owner can
     set soft/hard limits; 429-style response when exceeded.
5. **Admin surface**
   - CLI: `invincible user add/remove/list`, `invincible user revoke-key`.
   - (Web UI comes in Phase 5.)

### Dependencies

- **Phase 1** (audit log + identity groundwork — this phase builds on it).

### Acceptance criteria

- Two users on one gateway see disjoint sessions and usage.
- The audit log (Phase 1) records the acting user for every approval.
- Owner can create/remove users and set quotas entirely from the CLI.

---

## Phase 5 — Dashboard

**Priority P2 · Size L · Status: Planned · Requires: Phase 4**

### Goal

A local web UI (served by the FastAPI app) showing the gateway at a
glance, owned by the same `INVINCIBLE_OWNER_SECRET` browser-login flow the
OAuth consent page already uses.

### Problem it solves

Health today is CLI-only (`invincible doctor`, logs). Operators — and
multi-user owners (Phase 4) — want live state without a terminal.

### Scope

1. **Read views**
   - Provider health: cooldowns, disable state, tier order, last
     error/failure times (from `core/provider_health.py`).
   - Failover statistics: requests, 429s, 5xx, timeouts per provider.
   - Sessions: list, sizes, last activity (from `core/session_store.py`).
   - Pending approvals: staged `execute_bash`/`write_file` actions with
     approve/deny buttons (a real human-approval surface — closes the
     SECURITY.md "approval is remote" gap).
   - Audit log viewer (Phase 1 data).
2. **Where it lives**
   - Static HTML/JS served by FastAPI (no build step) under `/dashboard`,
     gated by the existing owner-session cookie; reuse the
     `/oauth/authorize` owner login.
3. **Extend later** — per-user usage panels once Phase 4 quotas exist.

### Dependencies

- Phase 1 (audit data to view), Phase 4 (users/usage to show). Could ship
  an owner-only version before Phase 4; the user panels wait for it.

### Acceptance criteria

- Owner can see provider health and pending approvals in the browser.
- Approve/deny from the dashboard records the same audit rows as `/mcp`.
- No new build tooling in the repo.

---

## Phase 6 — More providers (Done)

**Priority P2 · Size S · Status: Done · Prerequisite: none**

### Goal

Adding an upstream provider is a documented, tested 10-minute task — no
code changes.

### Problem it solves

`providers.yaml` already separates provider config from code, but the
router (`core/router.py`) hardcodes some assumptions (timeout blocks,
OpenAI-compatible base URLs). Providers outside that shape are awkward.

### Scope

1. **Provider plugin schema**
   - Formalize the `providers.yaml` schema (schema validation at load with
     clear error messages — mirrors `test_timeouts.py`'s "guards YAML
     typos" philosophy).
   - Per-provider request/response hooks (e.g. Gemini-flavored endpoints,
     key-in-query auth) behind a tiny extension point, or document which
     shapes are supported vs. not.
2. **Model aliasing**
   - Map a friendly alias → provider/model so users can ask for
     `"fast"` / `"strong"` without knowing the provider table.
3. **Template + docs**
   - A `docs/PROVIDERS.md` how-to and a commented `providers.yaml.example`.
   - Reference tier table stays in sync with the README's shipped order.
4. **Test fixture** — adding a fake provider in tests must be as easy as
   the existing `make_router` handlers. (Done — `default_providers()` +
   `make_router`; covered by `tests/test_provider_schema.py` and the Phase 6
   additions in `tests/test_router.py`, `tests/test_api.py`,
   `tests/test_anthropic_api.py`.)

### Dependencies

- None.

### Acceptance criteria

- Adding a plain OpenAI-compatible provider = editing `providers.yaml`
  only; documented in the site's Providers section (Phase 3).
- Invalid provider configs fail startup with a named, fixable error.

---

## Phase 7 — More MCP tools

**Priority P2 · Size M · Status: Planned · Prerequisite: none**

### Goal

A growing, safe tool catalog — every new tool following the same
approval/denylist/audit pattern as the existing three.

### Problem it solves

`read_file`, `execute_bash`, `write_file` cover the basics; real use
surfaces needs for read-only exploration and orchestration helpers that
are too fine-grained to justify a bash call.

### Scope

1. **Tool template**
   - A `docs/MCP_TOOLS.md` how-to: how a tool is declared, how it stages,
     how it is gated, how it is tested — with a copy-paste skeleton.
   - Refactor `core/tool_executor.py` so new tools register declaratively
     (name, schema, gate level) instead of each being bespoke.
2. **Candidate tools (examples)**
   - `list_files` / `read_dir` — read-only, ungated like `read_file`.
   - `grep_files` — bounded read-only search.
   - `git_status` / `git_log` — read-only git helpers.
   - `save_snippet` / `append_file` — gated like `write_file`.
   - Each candidate goes through the staging/approval/denylist pipeline;
     none bypasses it.
3. **Notifications** — keep the "no id → 204" behavior for new tools.

### Dependencies

- None; Phase 1's audit log should record new tools' approvals too (design
  the audit table in Phase 1 with a generic `action_type`).

### Acceptance criteria

- At least 3 new tools ship, all covered by tests, all using the same
  approval/denylist path.
- The how-to doc lets a contributor add a tool without reading the router.

---

## Phase 8 — Deployment

**Priority P3 · Size M · Status: Planned · Prerequisite: none**

### Goal

Run Invincible as a service, not just a foreground process: Docker for
portability, systemd for a dedicated Linux box, and a documented
cloud/tunnel story.

### Problem it solves

`invincible start` is great for a laptop; a headless server or team use
needs supervision, auto-restart, and predictable networking.

### Scope

1. **Docker**
   - `Dockerfile` (python:3.11-slim, non-root user, `pip install
     invincible-ai`).
   - `docker-compose.yml`: the **app + postgres service pair already
     exists from Phase 16** — this phase hardens it for production
     (named volumes, healthchecks with dependencies, restart policies).
2. **systemd unit**
   - Example unit: `ExecStart=invincible start --no-tunnel`, hardening
     options (`NoNewPrivileges`, `ProtectHome`), env file pointing at the
     config.
3. **Docs**
   - Deployment page on the site (Phase 3) with the three options:
     laptop (`invincible start`), Docker, systemd.
   - Cloud-hosted option: document running behind a reverse proxy +
     tunnel as a supported path (or explicitly out of scope — decide and
     say so).

### Dependencies

- None.

### Acceptance criteria

- `docker compose up` yields a healthy `/` on a fresh machine.
- The systemd unit survives reboot and crash (Restart=on-failure).
- Both recipes are documented and smoke-tested.

---

## Phase 9 — Context compression (Done)

**Priority P1 · Size M · Status: Done · Prerequisite: none (soft-ordered before Phase 10)**

### Goal

Shrink what is actually sent to a provider on each request — tool outputs
and other verbose content — *before* `trim_messages` decides what fits, so
fewer turns are dropped and fewer tokens are burned per call.

### Problem it solves

Every request resends the full trimmed history with no compression, and the
trim budget is computed on uncompressed sizes. Today trimming mostly bites
on failover to the 128k-context provider (`groq-llama` in
`providers.yaml`; the other shipped providers are 1M), so a single failover
silently discards most of a long session. Compression attacks both halves:
smaller payloads, and strictly more turns surviving the same budget.

### Scope

1. **Send-time compression pass** (`core/router.py`)
   - A transform applied to the in-memory message list *before*
     `trim_messages`' budget check, in both `route_request` and
     `stream_open` (identical behavior in streaming and non-streaming).
   - **Never persisted** — stored history stays verbatim. Compressing
     stored turns would progressively degrade them on every round trip.
2. **Compression rules**
   - e.g. tool-result truncation/deduplication, whitespace and verbosity
     reduction; behind a config toggle (`providers.yaml` or env var) —
     decide the default and document it.
   - Must preserve message structure: roles, and the `tool_calls` /
     tool-result pairing that `group_into_turns` treats as atomic.
3. **Token accounting**
   - `estimate_tokens` / the trim budget must be computed on
     post-compression sizes, or trimming still over-drops.
   - Anthropic `input_tokens` (`endpoints/anthropic_compat.py`
     `estimate_token_sum`) is currently computed on the untrimmed,
     uncompressed `full_messages` — adjust it to reflect what is actually
     sent, or document the drift explicitly.
4. **Tests** — compression keeps roles/structure intact, tool-turn
   atomicity survives, savings are measurable on a tool-heavy fixture,
   and stream/non-stream paths behave identically.

### Dependencies

- Phase 0. No storage, identity, or audit involvement — fully parallel to
  Phases 1 and 4.

### Acceptance criteria

- Payload token estimates are measurably smaller on tool-heavy sessions
  with message structure intact.
- For the same input, `trim_messages` keeps strictly ≥ as many turns after
  compression as before.
- Stored session history is byte-identical with compression on vs. off
  (send-time only).
- Full test suite stays green; SSE behavior unchanged.

---

## Phase 10 — Context memory (Done)

**Priority P2 · Size M · Status: Done · Soft ordering: after Phase 9 (not a hard dependency)**

### Goal

Facts from turns that trimming drops stop being lost forever: extract
simple `(entity, relation, target)` facts via pattern matching, store them
in SQLite, and inject a compact, size-bounded summary back into future
requests.

### Problem it solves

`trim_messages` drops the oldest turns silently and permanently — the
provider never sees them again, and the gateway keeps no record of what
was dropped. Separately, the session DB grows unboundedly: trimming is
send-time only and nothing ever shrinks the stored history.

### Scope

1. **Facts table** (new table in the session DB; `core/session_store.py`
   or a new `core/memory.py`)
   - Keyed `(user_id, session_id)` from day one, with `user_id`
     defaulting to a sentinel (`"default"`) until Phase 4 populates it
     from auth — Phase 4 then backfills instead of rebuilding.
   - If Phase 1's optional sessions-at-rest encryption lands first, the
     facts table inherits the same toggle.
2. **Extraction at persist time** (`endpoints/openai_compat.py`,
   `endpoints/anthropic_compat.py` `_persist`)
   - Runs when new turns are appended: idempotent, once per turn,
     protocol-agnostic.
   - Deliberately **not** in the router: `trim_messages` runs per provider
     attempt, so extracting "before a turn is dropped" there would extract
     different facts per provider and re-extract on retries.
3. **Injection at load time**
   - A compact facts summary injected as a system message — with its own
     explicit size bound, because system messages are never trimmed and an
     unbounded summary would become the new growth problem.
4. **Storage retention cap**
   - Bound the session DB's growth (e.g. max stored turns per session,
     with extracted facts preserving what rolls off); decide the policy
     and document it.
5. **Tests** — extraction idempotency, both protocols covered, injection
   respects the size cap, sentinel-key design documented for Phase 4.

### Dependencies

- Phase 0. Soft-ordered after Phase 9 so extraction design is validated
  against post-compression trim behavior, but not code-dependent on it.
- Survives Phase 4 via the sentinel `user_id` key (backfill, not rebuild).

### Acceptance criteria

- Facts from dropped turns are retrievable and injected within the size
  cap on subsequent requests.
- Extraction is idempotent (re-persisting a turn adds no duplicate facts)
  and works identically on both endpoints.
- Phase 4 migration of the facts table is a documented backfill, not a
  schema rebuild.
- Session DB size is bounded by the documented retention policy.
- Full test suite stays green.

---

## Phase 11 — Repo hygiene & packaging cleanup (Done)

**Priority P1 · Size S · Status: Done (lockfile strategy deferred — see below) · Prerequisite for: Phase 12**

### Goal

A clean repo and packaging baseline before any refactor work starts.

### Problem it solves

- Ad-hoc scripts sit at the repo root: `_test_tr.py` (untracked) and
  `anthropic_test.py` (**git-tracked**, contains a hardcoded gateway
  token string). Both bypass pytest; one ships a secret-shaped string.
- Test dependencies (`pytest`, `pytest-asyncio`) are mixed into runtime
  `requirements.txt`; there is no lockfile strategy.
- The deprecated root-level `providers.yaml` fallback chain still lives
  in `core/router.py`'s config resolution — three copies of provider
  config exist (packaged, root, example).
- `.gitignore` lists `sessions.db` twice.

### Scope

1. **Delete root scripts** — remove `_test_tr.py` and `anthropic_test.py`
   and drop the `anthropic_test.py` ignore entry from `pytest.ini`.
2. **Remove the root providers.yaml fallback** — packaged copy is
   authoritative (loaded via `importlib.resources`); delete the
   deprecated root file and its resolution branch; keep
   `providers.yaml.example`.
3. **Dev dependencies** — move pytest/pytest-asyncio into
   `[project.optional-dependencies].dev`. `ruff` is **already** a dev
   extra — nothing to do there.
4. **Lockfile strategy — deferred.** `uv` is not installed on the
   development machine and pip-tools was not adopted in this pass;
   `requirements.txt` remains hand-maintained pins with no lock
   verification. Revisit as a follow-up (or fold into Phase 13's
   Settings/tooling pass).
5. **Gitignore dedupe** — single `sessions.db` entry; confirm build
   artifact patterns cover everything currently on disk untracked.
6. **Version sync** — `invincible/__init__.py`'s `__version__ = "0.1.0"`
   has drifted behind the `v0.2.0` git tag, and
   `endpoints/mcp.py:166` separately hardcodes `"version": "0.1.0"` in
   the MCP `initialize` response instead of importing `__version__`.
   Bump `__version__` to match the tag and have `mcp.py` import it so
   the two can't drift again.

### Dependencies

- None (first phase of the new sequence).

### Acceptance criteria

- `git ls-files` shows no scripts or secret-bearing files outside
  `tests/`. ✅
- Fresh environment: `pip install -e .[dev]` is sufficient to run
  pytest and ruff. ✅
- Provider config resolves from exactly one place. ✅
- Lockfile strategy: not met — deferred (see scope item 4).

---

## Phase 12 — Correctness & security fixes (Done)

**Priority P1 · Size M · Status: Planned · Requires: Phase 11 · Prerequisite for: Phase 14**

### Goal

Close every verified bug before any structural refactor touches the same
files.

### Problem it solves

Four confirmed defects, found during a full code audit:

- Streamed OpenAI `tool_calls` are forwarded to the client but **lost
  from persisted history** — `_append_content`
  (`endpoints/openai_compat.py`) extracts only `delta.content`, while
  the Anthropic path preserves them correctly.
- Both failover loops treat **400 as retryable**
  (`core/router.py`, `route_request` and its streaming mirror), so a
  payload-invalid request burns every tier before surfacing.
- `GATEWAY_API_KEY` is compared with plain `==`/`!=`
  (`main.py::require_auth`), not a timing-safe digest.
- The login rate limiter's module-level failure dict grows without
  bound (`endpoints/oauth.py`).

### Scope

1. **Persist streamed OpenAI tool_calls** — accumulate tool-call deltas
   in `_stream_body` exactly as the Anthropic stream state machine does;
   persisted assistant turns become structurally complete. Tests must
   fail before the fix.
2. **400-failover policy (design decision, not a mechanical fix)** —
   HEAD currently forwards 400 verbatim and does not failover on it,
   matching the README and `test_upstream_error_forwarded`. An
   uncommitted working-tree change that made *all* 400s (and 410)
   failover was evaluated and reverted — blanket retry burns every
   tier on a malformed payload and is the opposite of the discrimination
   this item calls for. Failing over on 400 is *deliberate* for model
   mismatch (each tier serves a different model, so tier-1's "model not
   found" should cascade) but wrong for malformed payloads.
   **Landed as an explicit per-provider flag**: `failover_on_400: true`
   in `providers.yaml` (schema-validated boolean, default `false` =
   forward), applied identically in both failover loops; both classes of
   400 are tested.
3. **Timing-safe gateway key compare** — `hmac.compare_digest` in
   `main.py::require_auth` (supersedes Phase 1 item 2).
4. **Bound the login-failure map** — periodic sweep of stale IPs or a
   capped structure; document which.
5. **Explicitly out of scope:** the sync-sqlite3 `PendingActionStore`
   rewrite — absorbed into Phase 16's store migration.

### Dependencies

- Phase 11 (clean baseline).

### Acceptance criteria

- A streamed OpenAI tool-calling session round-trips through history
  with intact tool calls. ✅
- Payload-invalid requests abort fast without exhausting tiers;
  model-mismatch 400s still cascade (opt-in via `failover_on_400`) —
  both documented and tested. ✅
- Auth comparison is timing-safe; rate-limiter memory is bounded. ✅
- Full suite stays green. ✅ (426 passed; the conftest `client` fixture
  now wires `app.state.memory` explicitly, fixing pre-existing
  test-module state leakage that only alphabetical ordering hid.)

---

## Phase 13 — Failover engine unification & Settings

**Priority P1 · Size M · Status: Planned · Requires: Phase 14 (CI green first)**

### Goal

One failover engine instead of two hand-mirrored loops, and one typed
config surface instead of scattered env reads.

### Problem it solves

- `route_request` and `stream_open` duplicate ~100 lines of identical
  failover logic — the streaming path's docstring literally says it
  mirrors the non-streaming path "exactly". Every status-class change
  must be made twice (Phase 12 already pays this tax).
- `core/router.py` mixes schema validation, YAML loading, token
  estimation, turn trimming, and HTTP routing in one ~770-line module;
  `compat/common.py` imports token estimation from the Router even
  though the compat layer's own docstring declares independence from it.
- Roughly eleven env vars are read via scattered `os.getenv()` calls,
  alongside hardcoded cooldown curves, TTLs, timeouts, and context
  limits.

### Scope

1. **Single generator-based failover engine** parameterized by
   streaming vs non-streaming; `route_request` and `stream_open` become
   thin wrappers that only differ in how they consume attempt results.
2. **Split the module**: `core/config.py` (schema validation + YAML
   loading), `core/trimming.py` (`estimate_tokens`,
   `group_into_turns`, `trim_messages`), `router.py` keeps the Router,
   timeouts, and health wiring.
3. **Layering fix** — compat imports trimming helpers from
   `core/trimming.py`, never from the Router.
4. **Central Settings object** — typed, validated, owns every
   `INVINCIBLE_*` variable plus cooldown constants, TTLs, timeouts,
   context limits, and (forward-looking) `INVINCIBLE_DB_URL` and pool
   sizing for Phase 16.
5. **Typing pass** — hints throughout `invincible/`, strict ruff
   ruleset, mypy baseline clean.

### Dependencies

- **Phase 14 first**: the refactor must be protected by green CI.
- Soft-depends on Phase 12 landing (both touch the failover loops).

### Acceptance criteria

- Exactly one implementation of the failover decision sequence exists.
- No module under `invincible/compat/` imports from the Router.
- All configuration flows through Settings; no bare `os.getenv` outside
  it.
- Behavior identical pre/post (existing router tests pass unchanged);
  mypy and strict ruff clean.

---

## Phase 14 — Continuous integration

**Priority P1 · Size S · Status: Planned · Requires: Phase 11 · Partially done**

### Goal

Every pull request proven by machines, not discipline.

### Problem it solves

`.github/workflows/tests.yml` already exists (ruff check + pytest matrix
on 3.12/3.13/3.14, via `pip install -e ".[dev]"`) — this phase extends
existing CI rather than creating it from nothing. Gaps: no Postgres
service container, no coverage artifact, and the matrix (3.12–3.14)
does not match `pyproject.toml`'s `requires-python = ">=3.10"` —
3.10/3.11 are declared supported but never tested.

### Scope

1. **Extend the existing workflow** — keep ruff check + pytest; widen
   the Python matrix to match `requires-python` (add 3.10, 3.11) or
   narrow `requires-python` to match reality — decide and make the two
   consistent.
2. **Postgres service container job wired from day one** — initially
   runs the existing suite; when Phase 16's store-integration tests
   arrive they land in ready infrastructure instead of re-plumbing CI.
3. **Coverage artifact** per run; branch protection requires green
   checks before merge.

### Dependencies

- Phase 11 (dev deps separated so CI installs are honest).

### Acceptance criteria

- A red PR cannot merge.
- The matrix covers all versions declared in `requires-python`.
- Coverage report is downloadable from every run.

---

## Phase 15 — Observability

**Priority P2 · Size M · Status: Planned · Feeds: Phase 5**

### Goal

See what the gateway is doing without attaching a debugger or tailing
raw logs.

### Problem it solves

Observability today is log lines from `_log_attempt` plus a minimal
`/health` that returns version only. Operators cannot answer "which
provider is cooling down right now?" or "how many failovers happened
this hour?" — and Phase 5's dashboard needs exactly this data exposed
programmatically.

### Scope

1. **`/metrics`** (prometheus-client): request counts by route/status,
   upstream attempts by provider × outcome class, cooldown/disable
   gauges, stream durations, session-store timings.
2. **Request-ID middleware** — ID assigned per request, present in logs
   and error bodies, correlating multi-provider failover chains.
3. **Extend `/health`** with live provider health detail: tier order,
   cooldown expiry times, disable state, last failure timestamp
   *(promoted from the cross-cutting backlog)*.
4. **Readable console output** for `invincible start` — summarized
   request/failover lines instead of raw uvicorn noise *(absorbs the
   backlog item)*.

### Dependencies

- None hard; lands just before Phase 5 consumes it.

### Acceptance criteria

- Prometheus can scrape `/metrics` and render provider health gauges.
- A single request's full failover chain is reconstructible from logs
  via its request ID.
- `/health` reflects cooldown state accurately within one request of a
  failure occurring.

---

## Phase 16 — PostgreSQL storage migration

**Priority P1 · Size L · Status: Planned · Requires: Phase 13 (Settings) · Prerequisite for: Phases 1, 2, 4, 8**

### Goal

PostgreSQL becomes the project's single supported storage backend:
conversations, OAuth grants, facts, pending MCP actions, audit rows
(Phase 1), and provider health all live in Postgres.

### Decisions recorded up front

- **Postgres-only.** SQLite support is removed. Motivation: server/team
  deployment, Phase 4 write patterns, and modernization. This
  deliberately retires the original one-process SQLite-first posture —
  accepted tradeoff, since the laptop install story becomes
  "pipx + a DB URL or the bundled compose pair".
- **Data access:** SQLAlchemy 2.0 async Core over the asyncpg driver;
  Alembic for versioned migrations. Raw asyncpg was rejected to avoid
  hand-building pooling and migration tooling.
- **Migrations run explicitly:** `invincible db upgrade`, verified by
  `doctor` — never auto-run at startup.
- **Timestamps stay epoch floats** (`DOUBLE PRECISION`) initially;
  TIMESTAMPTZ conversion is deferred cleanup.
- **JSONB** for message history and redirect URIs.

### Problem it solves

SQLite caps concurrent writers, stores everything in a file the MCP
denylist must protect by name, forgets all health state on restart, and
cannot back multi-worker deployment. Four storage sites migrate:
`SessionStore`, `OAuthStore`, `MemoryStore` (shared connection today),
and `PendingActionStore` — which currently uses **sync sqlite3 inside
async paths** (`core/tool_executor.py`).

### Scope

1. **Dependencies** — add sqlalchemy[asyncio], asyncpg, alembic,
   greenlet; remove aiosqlite.
2. **Alembic baseline** creating: `sessions`, `facts`,
   `oauth_clients`, `oauth_codes`, `oauth_tokens`, `pending_actions`,
   `audit_log` (schema designed now, populated by Phase 1), and
   `provider_health`.
3. **Store rewrites preserving public APIs** — endpoints barely change:
   - `append()` becomes one transaction with `SELECT … FOR UPDATE` per
     session row (replacing the process-global asyncio.Lock
     load-modify-save).
   - Commit-per-operation becomes pooled transactions.
   - OAuth lazy-expiry DELETE-on-every-lookup becomes batched
     opportunistic cleanup.
   - `PendingActionStore` moves to an async PG table — persistence is
     now effectively free; revisit whether the opt-in persistence flag
     should default on.
4. **Config surface** — `INVINCIBLE_DB_URL` replaces
   `INVINCIBLE_DB_PATH`; CLI `--db-path` options across `start` /
   `oauth list|revoke|test-client` become DB-URL aware; pool sizing
   comes from the Phase 13 Settings object.
5. **CLI** — `setup` prompts for a DB URL or offers the bundled compose
   path; `doctor` gains connectivity + schema-version checks; new
   commands: `invincible db upgrade`, `invincible db import sessions.db`
   (one-shot legacy importer covering sessions, facts, and oauth rows).
6. **Test infrastructure** — the suite leans on SQLite `:memory:`
   fixtures (`tests/conftest.py`); Postgres has no equivalent.
   Introduce a real-PG fixture (testcontainers locally, the Phase 14
   service container in CI) with schema-per-test isolation.
7. **Compose pair** — `docker-compose.yml` with app + postgres
   services; consumed and hardened by Phase 8.
8. **Folded-in state persistence** — provider cooldowns/disables and
   the login rate-limit window survive restarts via PG tables
   *(promotes two backlog items)*.
9. **Docs** — README install story, CONFIGURATION.md, ARCHITECTURE.md
   module map, SECURITY.md: the denylist's file-protection of
   `sessions.db` loses meaning, the security boundary moves to DB
   credentials, and the DSN (which embeds a password) must be masked in
   doctor output and logs.

### Dependencies

- Phase 13 (Settings owns the DB URL and pool config).
- Phase 14 (PG service container + green-CI protection for an L-sized
  change).

### Acceptance criteria

- Full suite green against a real Postgres instance in CI.
- `invincible db import` round-trips a populated legacy `sessions.db`.
- `doctor` reports connectivity and schema revision; mismatches are
  loud.
- `docker compose up` yields a healthy gateway with zero manual SQL.
- Restarting the server preserves cooldowns, disables, and staged
  approvals; docs updated everywhere SQLite is mentioned.

---

## Cross-cutting backlog

Ideas that don't fit one phase cleanly. Pull from here when planning a
cycle.

*Recently promoted:* persistent cooldowns/disables and login-rate-limit
persistence → **Phase 16**; health-endpoint detail and prettier console
output → **Phase 15**.

- **Image content blocks** — Anthropic `image` blocks are currently
  skipped during flattening (`compat/anthropic.py`); OpenAI vision
  messages are untested upstream.
- **Per-key rate limiting on `/v1/*`** — today the only throttle is
  upstream providers' own 429s; one leaked key can burn every tier's
  quota unopposed. Precursor to Phase 4 quotas: per-key request/token
  windows enforced before routing.
- **`/v1` token revocation** — `GATEWAY_API_KEY` has no rotation/revoke
  path; a leaked chat key is good until `.env` is edited.
- **SSE robustness** — reconnect/keepalive behavior of the two streaming
  layers under flaky tunnels (`openai_compat.py`, `compat/anthropic.py`).
- **Graceful tunnel exit** — `start` already stops cloudflared with the
  server; verify behavior on Windows Ctrl+C vs. crash.
- **Usage/credit tracking** for the chat gateway (precursor to Phase 4
  quotas; could land earlier as `invincible usage`).
- **Vector / semantic memory** — embeddings + similarity retrieval over
  session history, the heavier successor to Phase 10's regex facts.
  Deferred pending two explicit decisions: embeddings (API provider vs.
  local model vs. Postgres full-text search) and store
  (`pgvector` inside the Phase 16 database vs. a dedicated service like
  Qdrant — a second long-running service is still a deployment question
  even though the SQLite-era "one-process" objection has been retired by
  Phase 16). Natural ordering: after Phase 4 (per-user index namespaces
  become natural) and Phase 8 (a vector service is a deployment question).

---

## Conventions for working on this roadmap

- A phase moves to `In progress` when its first PR lands; `Done` when its
  acceptance criteria all pass.
- Tasks pulled from the backlog are noted with `(backlog)` in PRs.
- Security-adjacent changes (Phase 1, anything touching `tool_executor.py`
  or `oauth_store.py`) update [SECURITY.md](SECURITY.md) in the same PR.
- Every phase PR ships tests; the suite stays green (`pytest`) and ruff
  clean.
