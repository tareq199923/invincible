# Adding Providers (Phase 6)

Adding a plain OpenAI-compatible provider is a **config-only** task: edit
`providers.yaml`, restart, done. No code changes.

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
- Anthropic: `{"model": "fast", "messages": [...]}` (echoed back in the
  response as before)

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
