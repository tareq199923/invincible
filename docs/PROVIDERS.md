# Adding Providers (Phase 6)

Adding a plain OpenAI-compatible provider **shape** is a **config-only**
task: edit `providers.yaml`, restart, done. No code changes.

## Where providers live now (read this first)

Since Phase 9 (BYOK), **live `/v1/*` traffic never reads
`providers.yaml`**. Every request routes through the calling user's own
connected credentials:

- A user connects a provider on the dashboard's **Providers page** with a
  single form: name + `base_url` + default `model_id` + key. A custom URL
  must pass the SSRF guard
  (`core/url_safety.py::validate_public_https_url`: public HTTPS only,
  and re-checked on every request because DNS can be rebound).
  (`core/provider_catalog.py` remains as the operator-supplied constants
  behind the API's `catalog_key` prefill, but the page renders no catalog
  cards.)
- Keys are Fernet-encrypted at rest under `INVINCIBLE_CREDENTIAL_KEY`, and
  the same surface is available over HTTP at `/providers/mine`. A remote
  deployment therefore needs **no provider keys of its own**.
- Each user then chooses their own routing — `auto`, `pinned`, or `chain` —
  stored per user in `user_settings` (see
  [CONFIGURATION.md](CONFIGURATION.md) → Routing modes).

`invincible/providers.yaml` is consequently a **static fixture**: the
packaged config the tests and direct `Router` construction use, the schema
`invincible doctor` validates, and the shape per-user credentials mirror.
The rest of this document is the reference for that schema and for adding a
provider *shape*. (`core/provider_catalog.py` still holds the
operator-supplied constants backing the API's `catalog_key` prefill.)

## The 10-minute task

1. Copy an existing entry in `invincible/providers.yaml`.
2. Set `name`, `tier`, `base_url`, `api_key_env`, `model_id`.
3. Put the key in your environment under `api_key_env`.
4. Restart (`inv start`). Run `inv doctor` first — it validates the file
   and reports named errors.

```yaml
providers:
  - name: my-provider
    tier: 5                        # ascending = failover order
    base_url: https://api.example.com/v1
    api_key_env: MY_PROVIDER_KEY
    model_id: my-model
    max_context: 32000
    timeout:
      read: 60.0
```

## Schema

Every provider entry supports:

| Field | Required | Type | Meaning |
|---|---|---|---|
| `name` | yes | string | Unique identifier, used in logs and health tracking. |
| `tier` | yes | int ≥ 1 | Failover priority; **ascending** (1 tried first). |
| `base_url` | yes | string | OpenAI-compatible base; must start `http://`/`https://`. |
| `api_key_env` | yes | string | Name of the env var holding the API key — never the key itself. |
| `model_id` | yes | string | Sent as `model` in the upstream payload. |
| `max_context` | no | int ≥ 1 | Token budget for trimming (default 32000). |
| `timeout` | no | mapping | Per-field httpx overrides (`connect`/`read`/`write`/`pool`). |
| `aliases` | no | list[str] | Friendly names clients can request (see below). Globally unique. |
| `auth_type` | no | `bearer` \| `query` | Default `bearer` (`Authorization: Bearer`). `query` puts the key in the URL. |
| `auth_param` | no | string | Query parameter name for `auth_type: query` (default `key`). |
| `chat_path` | no | string | Endpoint suffix (default `/chat/completions`); must start with `/`. |

**Unknown fields are rejected at startup** — a typo like `base_urll` fails
loudly instead of silently producing an unreachable provider.

## Validation

`load_providers_config` validates the whole file after YAML parsing, so
`inv start --config`, `inv doctor`, and the server all surface the same
named errors, e.g.:

```
Provider 'my-provider': 'tier' must be an integer >= 1
Duplicate alias 'fast' (providers 'groq-llama' and 'my-provider')
Provider 'my-provider': unknown field(s): base_urll
```

Rules:

- `providers` must be a YAML list (may be empty — the gateway then serves
  an empty `/v1/models` and 503s chat requests).
- Provider names and aliases must be unique across the file.
- Missing required fields name the provider and the missing fields.
- A provider whose `api_key_env` is unset in the environment is **skipped
  with a warning** at startup and request time — not a startup failure.

## Model aliasing

An alias is a **soft routing hint**: request `fast` and the aliased
provider moves to the front of the attempt order; if it fails, is in
cooldown, or is disabled, failover proceeds through the remaining tier
order exactly as before. An exact `model_id` match behaves the same way.

Requesting an unknown model name (e.g. Claude Code sending
`claude-sonnet-4`) changes nothing — normal tier order applies.

Aliases are accepted from both protocols:

- OpenAI: `{"model": "fast", "messages": [...]}`
- Anthropic: `{"model": "fast", "messages": [...]}` (the response reports the
  model that actually served — the provider's `model_id` — not the alias)

They also appear in `GET /v1/models`, listed after the real model ids, so
clients can discover them.

Shipped aliases:

| Alias | Prefers |
|---|---|
| `strong` | `nim-glm` |
| `fast` | `groq-llama` |
| `free` | `openrouter-fallback` |
| `backup` | `gemini-flash` |

## Supported provider shapes

The gateway routes to **OpenAI-compatible chat-completions JSON**:
`POST {base_url}{chat_path}` with a `{"model", "messages", "stream", ...}`
payload and a standard OpenAI JSON or SSE response. Providers outside that
shape (e.g. raw Gemini generateContent, key-in-header-only auth) are not
supported.

Two small hooks exist for providers close to the OpenAI shape:

- `auth_type: query` — sends the key as `?key=<key>` (or
  `auth_param`-named parameter) instead of an Authorization header.
  **Security note:** a key in the URL is visible to any proxy on the
  request path. This router never logs URLs, but prefer bearer auth
  wherever the provider supports it.
- `chat_path` — an endpoint suffix other than `/chat/completions`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Provider 'X' is missing required field(s): ...` | Add the named fields. |
| `Provider 'X': 'tier' must be an integer >= 1` | YAML quoted strings (`"1"`) and floats (`1.0`) are rejected — use a bare integer. |
| `Provider 'X': unknown field(s): ...` | Typos are rejected; check the schema table above. |
| `Duplicate alias ...` | Alias must be unique across providers. |
| Provider skipped at request time | The `api_key_env` var is unset; check `inv doctor`. |
---

## Aggregator quirks: opaque 400s (TokenRouter, Phase 13.5 case study)

Aggregators that proxy to many backend models sometimes wrap **their own
upstream failures** as an OpenAI-style `400` with a generic body:

```json
{"error": {"message": "openai_error",
           "type": "bad_response_status_code"}}
```

Confirmed live case (root cause proven by replaying the payload directly
against the provider, bypassing the gateway): the TokenRouter free tier
routed large/tool-heavy requests to an internal backend model the token
had no access to, returning
`403 - This token has no access to model <backend>` � surfaced through the
aggregator as a meaningless `400`.

**Recognition pattern**: the gateway logs the upstream body via
`_log_upstream_error_body`; if the message is generic (`openai_error`,
`bad_response_status_code`) while smaller/simpler requests to the same
provider succeed, suspect the aggregator's internal routing or tier
entitlements � not your payload.

**Mitigation**: set `failover_on_400: true` on the entry (already enabled
for TokenRouter) so affected requests degrade to the next tier instead of
failing client sessions. For definitive diagnosis, replay the exact
payload against the provider outside the gateway (see
`tools/replay_payload.py`), optionally with
`INVINCIBLE_DEBUG_400=1` to capture outgoing payloads per event.

## NVIDIA NIM quirks: empty tool-call ids & strict pairing (DeepSeek case study)

NVIDIA NIM (`https://integrate.api.nvidia.com/v1`, e.g. the
`nim-deepseek` entry in `invincible/providers.yaml` serving
`deepseek-ai/deepseek-v4-flash-0731`) is a vLLM-backed OpenAI-compatible
endpoint. Two documented vLLM failure modes combine here:

1. **Empty `tool_call` fields.** When vLLM's post-processing of a
   streamed tool call fails, the emitted delta can carry an empty or
   missing `id` (and occasionally `name`). This is seen most often on the
   SECOND tool call within one turn (e.g. a read_file call followed by a
   retry-read call in the same turn).

2. **Strict pairing validation (DeepSeek).** The model behind NIM rejects
   any request whose assistant `tool_calls` turn is not immediately
   followed by tool messages covering ALL of its ids:

   ```
   An assistant message with 'tool_calls' must be followed by tool
   messages responding to each 'tool_call_id'.
   (insufficient tool messages following tool_calls)
   ```

Together these made the second tool call of a turn fail deterministically
through the gateway: the second call's id was re-allocated during stream
assembly, so the persisted assistant turn carried one id while the client
saw (and answered with) another — upstream saw `assistant{tool_calls:[Y]}`
followed by `tool{X}` and 400'd. The same request reproduced through both
Codex (`/v1/responses`) and Claude Code (`/v1/messages`), proving the
defect was in gateway assembly, not the client or model.

**Gateway handling** (see [ARCHITECTURE.md § 3b](ARCHITECTURE.md)):

- tool-call ids are allocated exactly once per streamed call and reused
  on the wire, in the completed output, and in persistence;
- a reused stream index carrying a different id is a new call, not a
  merge;
- `repair_tool_pairing` validates/repairs the assembled message list
  before anything is routed and refuses to forward a half-paired list;
- `_iter_stream` buffers JSON split across `data:` lines so a chunked
  tool-call block no longer kills the stream.

**Diagnosis**: set `INVINCIBLE_DEBUG_400=1` (capture the outgoing payload
upstream rejected) and/or `INVINCIBLE_DEBUG_STREAM=1` (per-request raw
SSE chunk sequence + assembled tool states, keyed by the request id from
`x-invincible-request-id` and every attempt log line).
