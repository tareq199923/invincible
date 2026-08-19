# Configuration

Two things configure Invincible: environment variables (secrets and
behavioral flags) and `providers.yaml` (the upstream provider list). There is
no `config.json` — everything is env vars plus YAML.

---

## 1. Environment variables (`.env`)

See `.env.example`. Loaded via `python-dotenv` in `invincible/main.py`
(`load_dotenv()` at import time) and by the CLI's `start` command.

| Variable | Required by | Purpose |
|---|---|---|
| `GATEWAY_API_KEY` | `/v1/*` | Bearer token for the chat endpoint. **If unset, the endpoint is open (no auth).** |
| `INVINCIBLE_OWNER_SECRET` | `/oauth/authorize` | One-time **browser login** to approve MCP connections (30-day signed session cookie). **Not** sent on `/mcp` — requests use short-lived OAuth Bearer tokens. **If unset, no new MCP grants can be approved.** Legacy alias `MCP_SHARED_SECRET` is read as a fallback (~30 days, one-time deprecation notice). |
| `NVIDIA_API_KEY` | provider tier 1 | NVIDIA NIM hosted: GLM-5.2 (Z.ai); strongest coding/agentic tier. |
| `GROQ_API_KEY` | provider tier 2 | Groq Llama 70B. |
| `OPENROUTER_API_KEY` | provider tier 3 | OpenRouter free fallback. |
| `GEMINI_API_KEY` | provider tier 4 | Gemini Flash — last resort. |
| `INVINCIBLE_CONFIG_PATH` | startup | Path to a custom `providers.yaml` (set by CLI `--config`). |
| `INVINCIBLE_DB_PATH` | startup | Path to the session database (set by CLI `--db-path`). |
| `INVINCIBLE_PERSIST_PENDING_ACTIONS` | startup | **Opt-in**: when set, staged `execute_bash`/`write_file` approvals are written to the session database and survive a server restart. **Off by default** — pending actions are memory-only and a restart orphans them (clean slate). |
| `INVINCIBLE_COMPRESSION` | per-request | Send-time request compression (truncate long tool results, collapse blank-line runs) applied before context trimming. **On by default**; set to `0`/`false`/`off` to disable. Affects only what is sent to providers — stored session history stays verbatim. |

The two secrets are **independent**: a leaked tunnel URL alone is not enough
to reach tool execution, and rotating one secret never affects the other.

### Generating keys: `invincible setup`

`invincible setup` (Click CLI) creates/updates `.env`:

- Missing secrets (`GATEWAY_API_KEY`, `INVINCIBLE_OWNER_SECRET`) are
  generated as random `secrets.token_urlsafe(32)` values and written
  straight to the file (never echoed to the terminal).
- A legacy `MCP_SHARED_SECRET` in the existing `.env` is carried over to
  `INVINCIBLE_OWNER_SECRET` automatically (the same value keeps working).
- Existing values are kept unless `--force` is passed; prompts are
  `hide_input` so keys never appear in the shell.
- Inline comments and unrelated lines in an existing `.env` are preserved
  when rewriting (quoted and unquoted values both supported).
- Provider keys are prompted with "leave empty to skip".

---

## 2. `providers.yaml`

The canonical copy is **packaged**: `invincible/providers.yaml`, loaded via
`importlib.resources`. It works identically from a Git checkout, an editable
install, or a wheel. A deprecated compatibility copy exists at the
repository root (`providers.yaml`) and is used **only** if the packaged
resource cannot be read — it is not authoritative; do not edit it expecting
it to take effect.

### Schema

Top-level mapping with a single `providers:` list. Each provider entry:

```yaml
providers:
  - name: nim-glm                # unique, used in logs & health tracking
    tier: 1                        # ascending order = failover order
    base_url: https://integrate.api.nvidia.com/v1
    api_key_env: NVIDIA_API_KEY    # env var *name* — never the key itself
    model_id: z-ai/glm-5.2
    aliases: [strong]              # optional; soft routing hints (Phase 6)
    max_context: 1000000           # tokens; used for context trimming
    timeout:                       # optional; per-field override (see below)
      read: 90.0
```

| Field | Required | Type | Meaning |
|---|---|---|---|
| `name` | yes | string | Display/log identifier. |
| `tier` | yes | number | Failover priority; **ascending** (1 tried first). |
| `base_url` | yes | string | Provider's OpenAI-compatible base; the router posts to `{base_url}{chat_path}`. |
| `api_key_env` | yes | string | Name of the env var holding the API key. A provider whose key is unset is **skipped with a warning** (and any provider missing a required field raises `ValueError` at startup). |
| `model_id` | yes | string | Sent as `model` in the payload. |
| `aliases` | no | list[string] | Friendly names clients can request (soft routing hint; see below). Globally unique. |
| `max_context` | no | number | Token budget for trimming (default 32000). |
| `timeout` | no | mapping | Per-field httpx timeout overrides (see below). |
| `auth_type` | no | `bearer` \| `query` | Default `bearer` (`Authorization: Bearer`). `query` sends the key in a URL query parameter. |
| `auth_param` | no | string | Query parameter name for `auth_type: query` (default `key`). |
| `chat_path` | no | string | Endpoint suffix (default `/chat/completions`); must start with `/`. |

### Timeout resolution

Defaults (`DEFAULT_TIMEOUT_CONFIG` in `invincible/core/router.py`),
overridden field-by-field by each provider's `timeout:` block:

| Field | Default | Meaning |
|---|---|---|
| `connect` | 5.0s | Establishing the TCP/TLS connection. |
| `read` | 60.0s | Waiting for the response body. |
| `write` | 5.0s | Sending the request body. |
| `pool` | 2.0s | Acquiring a connection from the pool. |

Shipped values and rationale:

| Provider | Read timeout | Why |
|---|---|---|
| `nim-glm` | 90.0s | 1M-token context; generation can legitimately take a while. |
| `groq-llama` | 45.0s | Large model, still generous. |
| `openrouter-fallback` | 90.0s | 550B free tier can be slow to first token. |
| `gemini-flash` | 90.0s | 1M-token context; generation can legitimately take a while. |

### Provider validation (at `Router` construction)

- The full schema is validated after YAML load (Phase 6): missing required
  fields, wrong types (`tier` must be a bare integer ≥ 1), a `base_url`
  that isn't `http(s)://`, non-positive timeouts, **unknown fields**,
  **duplicate provider names**, and **duplicate aliases** each raise a
  `ValueError` naming the provider and field.
- Providers are sorted by `tier` ascending at construction.
- Missing API keys only produce a `logger.warning` at startup and a skip at
  request time — not a startup failure.
- A missing explicit `--config` path raises `FileNotFoundError`; malformed
  YAML raises `ValueError`.
- The same validation runs for `inv start --config` pre-flight, `inv
  doctor`, and the server — one code path, same errors.

### Model aliasing (Phase 6)

An optional `aliases:` list on a provider adds friendly names clients can
request. Routing is **soft**: the aliased provider moves to the front of
the attempt order; if it fails, is in cooldown, or is disabled, failover
continues through the rest of the tier order exactly as before. An exact
`model_id` match behaves the same way. Unknown model names (e.g. Claude
Code's `claude-sonnet-4`) change nothing — normal tier order applies.

Both protocols accept it: OpenAI `{"model": "fast", ...}` and Anthropic
`{"model": "fast", ...}` (echoed back in the response as before). Aliases
also appear in `GET /v1/models` after the real model ids. Full guide and
supported provider shapes: [docs/PROVIDERS.md](PROVIDERS.md).

---

## 3. Session database

- Default path: `sessions.db` in the **current working directory** (never
  inside the installed package).
- Override with `INVINCIBLE_DB_PATH` or the CLI flag
  `invincible start --db-path <path>`.
- Single table: `sessions (session_id TEXT PRIMARY KEY, messages TEXT,
  updated_at REAL)` — the whole conversation is one JSON blob per session id,
  replaced (upsert) on every save.
- **Plaintext, no encryption.** `sessions.db` is gitignored; the `write_file`
  and `read_file` denylists protect it from the MCP tools (see
  [docs/SECURITY.md](SECURITY.md)).

---

## 4. CLI reference

```
invincible setup [--env-file PATH] [--force]
invincible secret rotate [--env-file PATH] [--show]
invincible start [--host 127.0.0.1] [--port 8000] [--reload]
                [--log-level info] [--env-file .env]
                [--config PATH] [--db-path PATH]
invincible oauth list | revoke <client_id> | test-client [--db-path PATH]
invincible --version | --help
```

Notes on `secret rotate`:

- Regenerates `INVINCIBLE_OWNER_SECRET` inside the `.env` file in place
  using the same generation/rewrite machinery as `setup` — every other
  line, comment, and ordering is preserved, and the value is never echoed
  (use `--show` to print it deliberately).
- If the file still has the legacy `MCP_SHARED_SECRET` key, the new value
  is written under `INVINCIBLE_OWNER_SECRET` and the legacy line is
  removed, completing the migration.
- Requires an existing owner secret (new or legacy key) — running it before
  `invincible setup` prints guidance instead of creating a partial file.
- Does **not** revoke already-issued OAuth grants; use
  `invincible oauth revoke <client_id>` for that.

Notes on `start`:

- `--env-file` is loaded with `override=False` — a live process environment
  variable always wins over the file.
- `--config` validates the YAML up front (raising a CLI error on
  `FileNotFoundError`/`ValueError`) and sets `INVINCIBLE_CONFIG_PATH`.
- `--db-path` sets `INVINCIBLE_DB_PATH`.
- Binding to `0.0.0.0` prints a reminder of the local access URL.
- Both console scripts (`invincible` and `inv`) declared in `pyproject.toml`
  point at the same `cli` group.

Notes on `oauth revoke`:

- Client ids are random URL-safe strings and may start with `-`. Because
  Click parses such an id as an option, pass it after a `--` separator:
  `invincible oauth revoke -- <client_id>`. Same surface for `oauth list`
  and `oauth test-client` (both accept `--db-path`).
