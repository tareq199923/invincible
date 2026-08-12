# MCP Protocol — client-facing spec

This is the contract for anything that wants to call Invincible's tools over
HTTP: a cloud-hosted AI reaching your machine through a tunnel, a script, or
a manual `curl`. The server speaks a **minimal JSON-RPC 2.0 subset** over a
single `POST /mcp` — it is not a general-purpose MCP transport (no
streaming/SSE, no subscriptions, no batch).

---

## 1. Transport & auth

```
POST /mcp
Content-Type: application/json
X-MCP-Secret: <MCP_SHARED_SECRET>
```

- **Auth**: the `X-MCP-Secret` header must equal `MCP_SHARED_SECRET`
  (timing-safe comparison). Wrong/missing → `401`. If the secret is unset on
  the server → `503` (disabled, never open).
- **One request per HTTP POST.** The body is a single JSON-RPC 2.0 object.
- Protocol version advertised: `2025-06-18`.

---

## 2. Methods

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

Response: `result.tools` is an array of four tool descriptors:

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
    "content": [{"type": "text", "text": "{'status': 'read', 'path': 'C:\\\\Users\\\\me\\\\project\\\\notes.txt', 'content': '...'}"}],
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
    "content": [{"type": "text", "text": "{'status': 'error', 'error': 'File not found: ...'}"}],
    "isError": true
  }
}
```

Denylisted target (`.env`, `sessions.db`, `.git/`):

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
    "content": [{"type": "text", "text": "{'status': 'pending_confirmation', 'token': 'bXfQ...9aZt', 'action': 'execute_bash', 'command': 'git status', 'message': 'Call confirm_action with this token (approve=true/false) to proceed.'}"}],
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
    "content": [{"type": "text", "text": "{'stdout': '...', 'stderr': '', 'returncode': 0}"}],
    "isError": false
  }
}
```

- `approve: false` → `isError: true`, text `Declined.` Nothing runs.
- Denylist hit → the *first* call already returns `isError: true`, text
  starting `Blocked: <reason>` — no token is ever issued.

> Note: the result is the Python `dict.__str__()` output, so expect single
> quotes and Python escapes inside the JSON text field. `str(result)` is used
> for all three tools.

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
    "content": [{"type": "text", "text": "{'status': 'pending_confirmation', 'token': 'qWx2...Kp7', 'action': 'write_file', 'path': 'C:\\\\Users\\\\me\\\\project\\\\scratch\\\\out.txt', 'content_length': 5, 'message': 'Call confirm_action with this token (approve=true/false) to proceed.'}"}],
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
    "content": [{"type": "text", "text": "{'status': 'written', 'path': '...', 'bytes': 5}"}],
    "isError": false
  }
}
```

Failure (e.g. permission denied, or the token was unknown/expired) →
`isError: true`: text `{'status': 'error', 'error': '<exception>'}` for an
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

## 3. Notifications (no `id`)

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

## 4. Error codes

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

## 5. End-to-end example (tunnel)

Expose the local server with a tunnel, e.g. Cloudflare:

```bash
cloudflared tunnel --url http://127.0.0.1:8000
# → https://random-name.trycloudflare.com
```

Then a remote AI calls:

```bash
curl -X POST https://random-name.trycloudflare.com/mcp \
  -H "X-MCP-Secret: $MCP_SHARED_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}'
```

Security notes for this setup:

- The tunnel URL alone is useless without the MCP secret (independent from
  `GATEWAY_API_KEY`).
- `read_file` needs no confirmation. `execute_bash`/`write_file` return a
  token; the command/file only materializes after a second
  `confirm_action` call with that token and `approve: true`. Whoever holds
  the MCP secret is the approver — there is no terminal prompt to gate it.
- See [docs/SECURITY.md](SECURITY.md) for the full threat model.
