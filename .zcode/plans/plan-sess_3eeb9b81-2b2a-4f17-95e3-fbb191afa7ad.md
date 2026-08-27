# Phase 9 — BYOK Provider Connections: Implementation Plan

## What exploration confirmed

- **Router** (`core/router.py`): `_iter_attempts` iterates `self._candidates(model)` → `attempt_order(snapshot, health, routing, model)` (`core/selection.py`, auto mode sorts by `tier`). Keys resolve lazily per attempt via `settings.provider_api_key(provider["api_key_env"])`; health is keyed by bare `provider["name"]`. Route metadata, run recording, trimming (`max_context`), and timeouts all read the provider dict.
- **Principals**: `/v1/*` is token-only (`Authorization: Bearer inv_…` or gateway key); legacy/anonymous map to the system local owner. BYOK chat routing therefore keys on `principal.kind == "api_key"` (legacy/anonymous stay on the operator pool exactly as today).
- **Dashboard realm**: `require_user_session`, `_page/_audit/_payload/_wants_html` live in `endpoints/accounts.py`; ownership delete pattern = identical 404 for foreign/unknown + `hx-swap="delete"` row removal on 204 (`memory.html`).
- **Migration 0006** uses an information_schema guard so fresh `create_all` DBs skip cleanly; migration tests build scratch DBs via `admin_pg` + `alembic_command.upgrade(cfg, target)` (`tests/test_migration_isolation.py`, `tests/test_cli_db.py`).
- No existing BYOK code anywhere; `cryptography` is not a dependency.

## Deliberate decisions (flagged, not asked)

1. **Schema addition**: one column beyond the spec's list — `key_masked Text NOT NULL server_default ''` (first-2 + last-4 hint computed once at create). Plaintext is never stored, so this is the only way to render a masked key after submission; it goes into revision 0007.
2. **Catalog = all five packaged provider types** (`tokenrouter`, `nvidia_nim`, `groq`, `openrouter`, `gemini`) as constants in new `core/provider_catalog.py`, mirroring `providers.yaml` base URLs/models/max_contexts (operator YAML may change; catalog stays stable operator-supplied constants that skip the SSRF guard).
3. **SSRF guard also runs at chat time**, not just create/test: injected per-attempt key resolver validates before decrypting. A DNS rebind between "add" and any later use then can't bypass it.
4. **Health scoping**: router gets a `_health_key(provider)` helper (`provider.get("health_id") or name`); BYOK candidates carry `health_id=f"byok:{credential_id}"` so two users with same-labeled providers never share cooldown state (and can't poison operator cooldowns). All ~7 tracker call sites switch to the helper; behavior for operator providers is unchanged (no `health_id` → name).
5. `/v1/models` keeps listing operator providers (spec is silent; out of scope, noted in docs).

---

## PR-A — Storage & encryption primitive (commit `Phase 9 (1/4): PR-A ...`)

1. `pyproject.toml`: add `"cryptography"` to dependencies.
2. `core/settings.py`: `def credential_key(self) -> str | None` accessor for `INVINCIBLE_CREDENTIAL_KEY` ("unset disables BYOK storage entirely — fail closed", docstring mirroring `admin_key`).
3. `core/db.py`: new table `user_provider_credentials`: `id` BigInteger Identity PK; `user_id` FK users indexed; `provider_name` Text; `catalog_key` Text nullable; `model_id` Text; `base_url` Text; `encrypted_api_key` LargeBinary; `key_masked` (see decision 1); `status` Text server_default `"untested"`; `last_tested_at` Float nullable; `created_at`/`updated_at` Float; `UniqueConstraint("user_id","provider_name", name="uq_user_provider_credentials_user_name")`; `Index("idx_user_provider_credentials_user", …)`.
4. New `invincible/migrations/versions/20260827_0007_byok_credentials.py`: revision `0007`, down_revision `0006`, `_has_table` information-schema guard (0006's `_has_column` pattern, table-level), guarded `CREATE TABLE` + indexes; downgrade drops the table. Fresh `create_all` DBs skip cleanly.
5. New `core/credential_crypto.py`: chooses **Fernet** (`cryptography.fernet`) over raw AES-GCM — AEAD, simpler, documented choice. `encrypt(str)->bytes`, `decrypt(bytes)->str` building a Fernet from `settings.credential_key()` per call (live-read convention); invalid/malformed env value → `CredentialKeyError`; wrong/rotated key during decrypt (`InvalidToken`) → generic `CredentialDecryptError` that callers catch (message names only that the configured master key doesn't match — no ciphertext/secret material).
6. `main.py` lifespan: loud warning next to `_warn_if_gateway_open()` when `INVINCIBLE_CREDENTIAL_KEY` is unset ("BYOK provider connections disabled").
7. `cli.py`: `invincible secret credential-key [--env-file] [--show]` under the existing `secret` group, generating a Fernet key into `.env` via the existing `_apply_env_updates` helpers (CLI is the documented launcher exemption; CONFIGURATION.md documents it).
8. Tests: `tests/test_credential_crypto.py` (round-trip; missing key → fail-closed error; wrong/rotated key → caught clear error, not uncaught crash), `tests/test_migration_byok.py` following `test_migration_isolation.py` (upgrade 0006→0007 creates table/index/constraint; downgrade removes; fresh-create_all equivalence). ROADMAP flips **Phase 9 → In progress** in this commit.

## PR-B — Connect/list/test/remove API (commit `Phase 9 (2/4): PR-B ...`)

1. `core/url_safety.py`: `validate_public_https_url(url, *, resolve=socket.getaddrinfo)` raising `UnsafeUrlError(reason)`. Rejects non-https schemes, `localhost`/dotless-local hosts, IP literals in blocked ranges, and every DNS-resolved address in blocked ranges: RFC1918, 127.0.0.0/8, 169.254.0.0/16 (incl. 169.254.169.254 metadata), 0.0.0.0/8, ::1, fe80::/10, fc00::/7 ULA. Injectable resolver keeps unit tests fully hermetic.
2. `core/credential_store.py` (`ByokCredentialStore(engine)`): thin SQLAlchemy Core repo — `create` (honors unique pair; duplicate → typed error → 400), `list(user_id)`, `get_for_user(id,user_id)`, `delete(id,user_id)→bool`, `update_test_outcome(id,status,last_tested_at)`, `routing_rows(user_id)` ordered by `created_at,id`.
3. New `endpoints/byok.py`, cookie-realm only (`Depends(require_user_session)` mirrored from dashboard.py; a second dependency `_require_credential_key` returns fail-closed **503 config_error** when `INVINCIBLE_CREDENTIAL_KEY` unset/unparseable — same posture as the admin surface):
   - `GET /dashboard/providers` HTML page; `GET /providers/mine` JSON (masked forms only).
   - `POST /providers/mine` via `_payload`-style JSON/form bodies; catalog entries take prefilled base_url/model from catalog key, skip SSRF; custom entries go through `validate_public_https_url`. Stores Fernet ciphertext + masked hint. Responses echo masked form only.
   - `POST /providers/mine/{id}/test`: ownership-predicated (identical 404 foreign/unknown), SSRF re-check, probe `GET {base_url}/models` reusing the `ProviderRegistry.test()` report shape (`{ok,status,latency_ms,detail}`) with Bearer auth from the decrypted user key; updates status/last_tested_at either way; outgoing httpx client injectable via `app.state.byok_http_client` so tests stay on MockTransport.
   - `DELETE /providers/mine/{id}`: exact `delete_memory` pattern (identical 404, audit `byok.credential.deleted`, HTMX 204-row-delete support).
   - Audit events `byok.credential.created/tested/deleted` via `_audit`; meta carries id/provider_name/catalog_key only.
   - Wired in `main.py`.
4. `docs/SECURITY.md`: credential-encryption model section (what's protected: DB-only compromise is inert without `INVINCIBLE_CREDENTIAL_KEY`; what isn't: DB+env compromise decrypts; no rotation yet — stated limitation, mirroring existing framing) + SSRF guard description; both added in this PR per AGENTS change discipline.
5. Tests `tests/test_byok_api.py`: anon/session/in_-key realm matrix (inv_ gets 401 on all four routes, mirroring `test_inv_api_key_never_authorizes_password_surface`); identical foreign/unknown 404s; masked key present / full key absent everywhere incl. responses; duplicate `(user_id, provider_name)` rejected; SSRF matrix (loopback/RFC1918/link-local-metadata/::1/ULA/http-scheme rejected with fake resolver; public host accepted); catalog skips guard; audit rows contain no secret material; test-endpoint updates ok/failed via injected fake upstream, never returning the key.

## PR-C — Router: per-user candidate pool (commit `Phase 9 (3/4): PR-C ...`)

1. `core/router.py`:
   - New `NoCredentialsConfiguredError(AllProvidersFailedError)` (subclassing keeps unknown call sites safely on the old exhaustion path while endpoints catch it first for the clean 400-class response).
   - `route_request*`/`stream_open*` gain keyword-only `byok_candidates: list[dict] | None = None` + `byok_key_resolver: Callable[[dict], Awaitable[str]] | None = None`, threaded into `_iter_attempts`.
   - In `_iter_attempts`: when `byok_candidates is not None`, empty list raises `NoCredentialsConfiguredError("No AI provider connected..."); connect one at /dashboard/providers."); otherwise candidates come from `attempt_order(byok_candidates, self.health_tracker, AUTO_ROUTING, model)` (tier/order semantics reused; rows pre-assigned `tier=created-index+1`). Key resolution becomes `await self._resolve_attempt_key(provider, byok_key_resolver)` — BYOK dicts carry no key; decryption happens once per attempt, lazily. `CredentialDecryptError` inside an attempt logs a warning and fails over like a missing key.
   - Health calls switch to `_health_key(provider)` (decision 3). Single-loop invariant preserved — transport mechanics/classification untouched.
2. Shared resolution helper (in `endpoints/byok.py`, imported by both compat modules — compat still never imports the Router): given `Principal` returns `(candidate_dicts, async resolver) | None` — `None` for local principals. Resolver = store lookup by credential id → `validate_public_https_url` again → decrypt → plaintext to headers only. Candidates carry `name` (user label), `base_url`, `model_id`, `max_context` (catalog default), `enabled=True`, `health_id="byok:<id>"`.
3. `endpoints/openai_compat.chat_completions` + both streaming/non-streaming paths of `anthropic_compat`: if principal kind == "api_key" and helper returns empty rows → return protocol-appropriate 400 before calling the Router (OpenAI `invalid_request_error` JSON 400; Anthropic `{type:"error", type:"invalid_request_error"}` 400) saying no provider is connected. Otherwise pass `byok_*` kwargs through.
4. Tests: `test_router_byok.py` (unit) + endpoint-level in `test_chat_byok.py` using registered account + minted `inv_` API key + MockTransport keyed by host:
   - BYOK request hits ONLY their providers' hosts; operator registry hosts' handler counters assert zero hits;
   - zero credentials → clean 400 both protocols, operator pool untouched;
   - their sole provider 401-ing/down → clean failure, never a silent hop to operator providers (the pinned product-decision test);
   - failover works across 3 of their own creds on 429→5xx→success; cooldown state scoped by credential id (two users, same label, no cross-contamination);
   - legacy gateway-key request completely unaffected.

## PR-D — Dashboard UI (commit `Phase 9 (4/4): PR-D ...`)

1. `templates/base.html`: nav gains `<a href="/dashboard/providers">Providers</a>` between Memory and Usage.
2. `templates/providers.html` mirroring `memory.html` idioms: catalog card grid (Connect opens an inline form with base URL/model pre-filled editable, only API key required input — password-field discipline, no echoed values), "+ Add custom provider" blank form, connected table (masked key, model, base URL, status badge untested/ok/failed, Remove via `hx-delete`+`hx-swap="delete"`+confirm exactly like memory.html, Test as normal POST redirect back with bounded query flash showing the last outcome — same pattern the settings page already pins). Forms POST the dual JSON/form body style accounts endpoints accept.
3. Tests `tests/test_dashboard_providers.py`: page 401 anon; inv_ key 401 on page + routes; full HTTP round-trip (connect → redirect → page shows masked form, submitted raw key string absent from every subsequent render — `>short<`-style pin); remove flow drops the row; test button flips status ok/failed against injected fake upstream client; status badges render.
4. Final docs sweep in this commit: `docs/ROADMAP.md` Phase 9 section (Phase-5-format Status/scope/acceptance paragraph) flipped to **Implemented** once acceptance criteria pass; `docs/CONFIGURATION.md` (INVINCIBLE_CREDENTIAL_KEY + `secret credential-key`); `docs/API_REFERENCE.md` (/providers/mine contract); `docs/TESTING.md` file-map additions; final SECURITY.md pass.

## Definition-of-done checks I will run explicitly

- ruff clean + full suite green (Postgres at 5433 assumed available; hermetic suites unaffected) after each commit.
- Coverage spot-check on the new modules (pytest-cov).
- Acceptance bullets exercised: session-only surfaces, identical 404s, no secret leakage paths (grep audits/logs/responses in tests), SSRF per-range passes, operator-pool isolation pinned, roadmap/docs updated in-PR.
