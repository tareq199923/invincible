# MCP Protocol — client-facing spec

This is the contract for anything that wants to call Invincible's tools over
HTTP: a cloud-hosted AI reaching your machine through a tunnel, a script, or
a manual `curl`. The server speaks a **minimal JSON-RPC 2.0 subset** over a
single `POST /mcp` — it is not a general-purpose MCP transport (no
streaming/SSE, no subscriptions, no batch).

Auth is **OAuth 2.1 + PKCE**: the `/mcp` endpoint is an OAuth resource
server, guarded by short-lived Bearer access tokens issued by Invincible's
own built-in authorization server. This matches what MCP-compatible clients
(the Claude app's "Add custom connector" flow, etc.) expect.

---

## 1. Discovery

### Protected-resource metadata (RFC 9728) — the MCP server

`GET /.well-known/oauth-protected-resource`:

```json
{
  "resource": "http://127.0.0.1:8000/mcp",
  "canonical_uri": "http://127.0.0.1:8000/mcp",
  "authorization_servers": ["http://127.0.0.1:8000"]
}
```

This is where a client starts after hitting a `401` with this header:

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer resource_metadata="http://127.0.0.1:8000/.well-known/oauth-protected-resource"
```

### Authorization-server metadata (RFC 8414) — the OAuth server

`GET /.well-known/oauth-authorization-server`:

```json
{
  "issuer": "http://127.0.0.1:8000",
  "authorization_endpoint": "http://127.0.0.1:8000/oauth/authorize",
  "token_endpoint": "http://127.0.0.1:8000/oauth/token",
  "registration_endpoint": "http://127.0.0.1:8000/oauth/register",
  "revocation_endpoint": "http://127.0.0.1:8000/oauth/revoke",
  "response_types_supported": ["code"],
  "grant_types_supported": ["authorization_code", "refresh_token"],
  "code_challenge_methods_supported": ["S256"],
  "token_endpoint_auth_methods_supported": ["none"],
  "revocation_endpoint_auth_methods_supported": ["none"]
}
```

---

## 2. Transport & auth

```
POST /mcp
Content-Type: application/json
Authorization: Bearer <access_token>
```

- **Auth**: the `Authorization: Bearer` token must be a live access token
  issued by the `/oauth` server. Missing, expired, or revoked → `401` with
  the `WWW-Authenticate` challenge above. There is no `X-MCP-Secret` header
  any more.
- **One request per HTTP POST.** The body is a single JSON-RPC 2.0 object.
- Protocol version advertised: `2025-06-18`.

> The `resource` parameter (RFC 8707) that MCP clients send on authorize /
> token requests is accepted and ignored — this deployment is a single
> authorization server for a single resource.

---

## 3. Connecting a client (OAuth flow)

A compliant MCP client performs: discovery → dynamic registration →
authorization-code flow with PKCE (S256) → Bearer calls on `/mcp`.

### 3.1 Register a client (RFC 7591)

```http
POST /oauth/register
Content-Type: application/json

{"redirect_uris": ["http://localhost:8765/callback"], "client_name": "my-agent"}
```

Response (`201`):

```json
{
  "client_id": "9zLm...WxQ",
  "client_name": "my-agent",
  "redirect_uris": ["http://localhost:8765/callback"]
}
```

Public client — **no `client_secret`** is issued (PKCE-only). Registration
is open (that is normal for dynamic registration); the real gate is the
consent page, so only a registered `redirect_uri` is ever redirected to.
Redirect URIs must be `https://` or loopback (`http://localhost` /
`http://127.0.0.1`).

#### Grok custom connector

The registration **response body is intentionally minimal** — only
`client_id`, `client_name`, and `redirect_uris`. Do **not** echo
unsolicited `token_endpoint_auth_method`, `grant_types`, `response_types`,
or `client_id_issued_at` without re-validating against Grok. A fuller
RFC 7591 §3.2.1 echo has been observed to make Grok abort after a
successful registration (client id stored, consent page never opened,
every subsequent `/mcp` call returns 401). Authorization-server metadata
still advertises `token_endpoint_auth_methods_supported: ["none"]` for
clients that follow discovery. Prefer a live Grok connector re-test before
expanding the registration response.

### 3.2 Authorize (consent page)

```
GET /oauth/authorize?response_type=code&client_id=<id>&redirect_uri=<uri>
    &code_challenge=<S256>&code_challenge_method=S256&state=<opaque>
```

- No valid owner session cookie → a **login form** asking for
  `INVINCIBLE_OWNER_SECRET` (entered once per browser, ~30-day remembered
  session). A wrong secret sets no cookie.
- Logged in → a **consent page**: "`<client_name>` wants access to your
  Invincible instance. [Approve] [Deny]".

On **Approve**, the owner's browser is redirected to:

```
http://localhost:8765/callback?code=<single-use-code>&state=<opaque>
```

On **Deny**:

```
http://localhost:8765/callback?error=access_denied&state=<opaque>
```

The code is single-use, bound to the exact client / redirect URI / PKCE
challenge, and expires after ~5 minutes. Invalid `client_id` or a
`redirect_uri` that was never registered is answered with an error page —
the server **never** redirects to an unregistered URI.

### 3.3 Exchange code for tokens

```http
POST /oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=authorization_code&code=<code>&client_id=<id>
&redirect_uri=<uri>&code_verifier=<verifier>
```

The `code_verifier` must hash (S256) to the `code_challenge` sent in 3.2.
Response (`200`):

```json
{
  "access_token": "74mC...",
  "token_type": "Bearer",
  "expires_in": 3600,
  "refresh_token": "aR2b..."
}
```

Access tokens expire after **1 hour**; refresh tokens after **30 days**.
Refresh tokens are **rotated**: every refresh invalidates the previous one.

### 3.4 Refresh

```http
POST /oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=refresh_token&refresh_token=<refresh_token>
```

Returns a fresh `access_token` and a **new** `refresh_token` (the old one
is revoked).

### 3.5 Revoke

```http
POST /oauth/revoke
Content-Type: application/x-www-form-urlencoded

token=<access-or-refresh-token>
```

Always `200` (an unknown token counts as already revoked).

---

## 4. Methods

### `initialize`

```json
{"jsonrpc": "2.0", "id": 1, "method": "initialize"}
```

Response:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "protocolVersion": "2025-06-18",
    "serverInfo": {"name": "invincible-mcp", "version": "0.1.0"},
    "capabilities": {"tools": {}}
  }
}
```

### `tools/list`

```json
{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
```

Response: `result.tools` is an array of seven tool descriptors (the
original file/exec/approval surface plus the Phase 15b continuity tools):

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "tools": [
      {
        "name": "read_file",
        "description": "Read a file's contents from the host machine. ...",
        "inputSchema": {
          "type": "object",
          "properties": {"path": {"type": "string"}},
          "required": ["path"]
        }
      },
      {
        "name": "execute_bash",
        "description": "Run a shell command on the host machine. ...",
        "inputSchema": {
          "type": "object",
          "properties": {"command": {"type": "string"}},
          "required": ["command"]
        }
      },
      {
        "name": "write_file",
        "description": "Write content to a file on the host machine. ...",
        "inputSchema": {
          "type": "object",
          "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
          "required": ["path", "content"]
        }
      },
      {
        "name": "confirm_action",
        "description": "Approve or deny a pending execute_bash/write_file request. ...",
        "inputSchema": {
          "type": "object",
          "properties": {"token": {"type": "string"}, "approve": {"type": "boolean"}},
          "required": ["token", "approve"]
        }
      },
      {
        "name": "task_state_set",
        "description": "Persist canonical task progress into the shared continuity store ...",
        "inputSchema": {
          "type": "object",
          "properties": {
            "payload": {"type": "string"},
            "task_key": {"type": "string"},
            "status": {"type": "string", "enum": ["active","blocked","done","cancelled"]},
            "expected_version": {"type": "integer"},
            "session_id": {"type": "string"}
          },
          "required": ["payload"]
        }
      },
      {
        "name": "task_state_get",
        "description": "Read the latest trusted task state ...",
        "inputSchema": {
          "type": "object",
          "properties": {"task_key": {"type": "string"}, "session_id": {"type": "string"}}
        }
      },
      {
        "name": "checkpoint_create",
        "description": "Snapshot the current task-state version as a named checkpoint ...",
        "inputSchema": {
          "type": "object",
          "properties": {"note": {"type": "string"}, "task_key": {"type": "string"}, "session_id": {"type": "string"}}
        }
      }
    ]
  }
}

```

### `tools/call`

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {"name": "<tool>", "arguments": { ... }}
}
```

`arguments` is optional and defaults to `{}`.

#### `read_file`

```json
"arguments": {"path": "C:\\Users\\me\\project\\notes.txt"}
```

No confirmation. Result (success):

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "content": [{"type": "text", "text": "{\"status\": \"read\", \"path\": \"C:\\\\Users\\\\me\\\\project\\\\notes.txt\", \"content\": \"...\"}"}],
    "isError": false
  }
}
```

Result (error, e.g. missing file):

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "content": [{"type": "text", "text": "{\"status\": \"error\", \"error\": \"File not found: ...\"}"}],
    "isError": true
  }
}
```

Denylisted target (`.env`, `sessions.db`, `.git/`):

> Live state (conversations, OAuth grants, task state) lives in PostgreSQL;
> the `sessions.db` denylist entry remains so leftover pre-Phase-16 files
> can never be read or written by these tools.

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "content": [{"type": "text", "text": "Blocked: read of Invincible's .env file (.env)"}],
    "isError": true
  }
}
```

#### `execute_bash`

```json
"arguments": {"command": "git status"}
```

If the command survives the denylist it is **staged, not run** — the call
returns immediately with a `pending_confirmation` result carrying a token:

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "content": [{"type": "text", "text": "{\"status\": \"pending_confirmation\", \"token\": \"bXfQ...9aZt\", \"action\": \"execute_bash\", \"command\": \"git status\", \"message\": \"Call confirm_action with this token (approve=true/false) to proceed.\"}"}],
    "isError": false
  }
}
```

Nothing has executed yet. The command only runs after a follow-up
[`confirm_action`](#confirm_action) call:

- `approve: true` → command runs (30s timeout; on timeout the process is
  killed and you get `returncode: -1` and a timeout message in `stderr`).

```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "result": {
    "content": [{"type": "text", "text": "{\"stdout\": \"...\", \"stderr\": \"\", \"returncode\": 0}"}],
    "isError": false
  }
}
```

- `approve: false` → `isError: true`, text `Declined.` Nothing runs.
- Denylist hit → the *first* call already returns `isError: true`, text
  starting `Blocked: <reason>` — no token is ever issued.

> Note: tool results are JSON-encoded with `json.dumps` — the text field is
> valid JSON (double quotes, no Python `None`/`True` literals). Parse it
> with a JSON decoder, not `ast.literal_eval`. This applies to all tools
> (`read_file`, `execute_bash`, `write_file`, `confirm_action`).

#### `write_file`

```json
"arguments": {"path": "C:\\Users\\me\\project\\scratch\\out.txt", "content": "hello"}
```

Like `execute_bash`: denylist first (path resolves inside the repo to
`.env*`, `providers.yaml`, `sessions.db`, `invincible/`, `tests/`, `.git/` →
`Blocked`, no token), then **staging** — the call returns a
`pending_confirmation` result with a token and `content_length`, and
nothing is written until [`confirm_action`](#confirm_action) approves the
token:

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "content": [{"type": "text", "text": "{\"status\": \"pending_confirmation\", \"token\": \"qWx2...Kp7\", \"action\": \"write_file\", \"path\": \"C:\\\\Users\\\\me\\\\project\\\\scratch\\\\out.txt\", \"content_length\": 5, \"message\": \"Call confirm_action with this token (approve=true/false) to proceed.\"}"}],
    "isError": false
  }
}
```

On approval parent directories are created automatically
(`os.makedirs(..., exist_ok=True)`). Success:

```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "result": {
    "content": [{"type": "text", "text": "{\"status\": \"written\", \"path\": \"...\", \"bytes\": 5}"}],
    "isError": false
  }
}
```

Failure (e.g. permission denied, or the token was unknown/expired) →
`isError: true`: text `{"status": "error", "error": "<exception>"}` for an
execution failure, or `Unknown or expired confirmation token.` /
`Declined.` for the approval outcome.

#### `confirm_action`

```json
"arguments": {"token": "bXfQ...9aZt", "approve": true}
```

Resolves a pending `execute_bash`/`write_file` request. `token` must be
the exact token from that request; `approve` must be a real JSON boolean
(a string like `"true"` is treated as deny).

| Outcome | Result |
|---|---|
| Approved (`approve: true`) | The real action result (`stdout`/`stderr`/`returncode` for bash; `status`/`path`/`bytes` for write), `isError: false`. |
| Declined (`approve: false`) | `isError: true`, text `Declined.` — entry discarded, nothing runs. |
| Unknown / expired / already-used token | `isError: true`, text `Unknown or expired confirmation token.` — nothing runs. |

Tokens are valid for **10 minutes** and are **single-use**: the first
`confirm_action` that resolves a token consumes it, so replaying the same
token can never execute the action twice.

---

## 5. Notifications (no `id`)

A request **without** an `id` field is a JSON-RPC 2.0 *notification*: the
server still performs the side effect (e.g. `tools/call` executes), but
replies `204 No Content` with an empty body — even when the call would have
errored. A `confirm_action` sent as a notification **does approve/deny** —
notifications are not "pings", they run normally. Session store, pending
actions, and all side effects are untouched by the missing `id`.

```json
{"jsonrpc": "2.0", "method": "tools/call",
 "params": {"name": "execute_bash", "arguments": {"command": "ls"}}}
```

→ `HTTP 204`, empty body.

---

## 6. Error codes

| Code | Meaning | When |
|---|---|---|
| `-32700` | Parse error | Body is not valid JSON. `id: null`. |
| `-32600` | Invalid Request | Body is not an object (e.g. a JSON array). `id: null`. |
| `-32602` | Invalid params | `params` exists but is not an object. |
| `-32601` | Method not found | Unknown `method`, or unknown tool name in `tools/call` (message: `Unknown tool: <name>` / `Unknown method: <method>`). |

Protocol-level errors are returned as JSON-RPC errors:

```json
{"jsonrpc": "2.0", "id": null, "error": {"code": -32700, "message": "Parse error"}}
```

Tool-level failures (blocked/declined/missing file) are **not** JSON-RPC
errors — they are successful calls whose `result.isError` is `true`.

---

## 7. End-to-end example (tunnel)

`invincible start` launches a named Cloudflare tunnel alongside the server
(`cloudflared tunnel run <name>`, default name `invincible`; override with
`--tunnel-name` or `INVINCIBLE_TUNNEL_NAME`, skip with `--no-tunnel`). Its
log lines — including the public URL when cloudflared prints one — appear
prefixed `[tunnel]`, and the tunnel is shut down when the server stops. A
tunnel that dies is reported as soon as it exits.

For a one-off quick tunnel instead (no named-tunnel config required):

```bash
cloudflared tunnel --url http://127.0.0.1:8000
# → https://random-name.trycloudflare.com
```

Without a valid token, `/mcp` answers `401` with the RFC 9728 challenge:

```bash
curl -i -X POST https://random-name.trycloudflare.com/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}'

# HTTP/1.1 401 Unauthorized
# WWW-Authenticate: Bearer resource_metadata="https://random-name.trycloudflare.com/.well-known/oauth-protected-resource"
```

A compliant MCP client then auto-discovers the authorization server and
runs the OAuth flow (§3) in the browser. For **manual testing without a
browser**, use the built-in helper:

```bash
invincible oauth test-client
# client_id:   9zLm...WxQ
# access token expires in 3600s
# curl -X POST http://127.0.0.1:8000/mcp \
#   -H "Authorization: Bearer <token>" \
#   -H "Content-Type: application/json" \
#   -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Or drive the flow by hand with a browser: open `/.well-known/oauth-protected-resource`,
register a client, approve the consent page, and use the returned token.

Security notes for this setup:

- The tunnel URL alone is useless — no valid token, no access.
- Access tokens expire in an hour and can be revoked immediately with
  `invincible oauth revoke <client_id>`.
- `read_file` needs no confirmation. `execute_bash`/`write_file` return a
  token; the command/file only materializes after a second
  `confirm_action` call with that token and `approve: true`. Whoever holds
  a valid bearer token is the approver — there is no terminal prompt to
  gate it.
- See [docs/SECURITY.md](SECURITY.md) for the full threat model.