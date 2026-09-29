# invincible/endpoints/docs.py
"""Public documentation site (flexx.dev/docs-style).

Curated, standalone pages served by the app itself: ``GET /docs`` renders
the introduction, ``GET /docs/{slug}`` renders one allowlisted guide.
Unknown slugs 404 — internal repo docs (audits, account transfers,
workqueues, gap analyses) are never published here.

Content discipline (AGENTS.md): docs follow implementation. CLI commands
mirror ``cli.py`` (``harness setup`` / ``harness connect``), the MCP tool
table is generated from the live ``TOOLS`` list in ``endpoints/mcp.py``,
and every behavior claim (BYOK-only, lexical memory, failover codes) matches
the tested semantics. No secrets are rendered.
"""
import re
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

from invincible.endpoints.template_filters import register_template_filters

router = APIRouter(tags=["docs"])

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
register_template_filters(templates)


def _md() -> MarkdownIt:
    # CommonMark + tables, raw HTML disabled so curated markdown can never
    # inject markup (we control the source, but belt and suspenders).
    return MarkdownIt("commonmark", {"html": False}).enable("table")


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "section"


def _toc(markdown: str) -> list:
    items = []
    for line in markdown.splitlines():
        m = re.match(r"^(#{2,3})\s+(.*)", line.strip())
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            items.append({"level": level, "title": title,
                          "anchor": _slugify(title)})
    return items


def _mcp_tools() -> list:
    """Live tool names + first-sentence descriptions (no drift)."""
    from invincible.endpoints.mcp import TOOLS
    rows = []
    for t in TOOLS:
        desc = (t.get("description") or "").split(".")[0].strip() + "."
        rows.append({"name": t["name"], "desc": desc})
    return rows


# ---------------------------------------------------------------------------
# Curated page bodies (markdown). Each stays scoped to shipped behavior.
# ---------------------------------------------------------------------------

_INTRO = """\
# Introduction to Invincible

Invincible is a remote-first, multi-user AI continuity platform: one stable
gateway for OpenAI, Anthropic, and Codex clients, routed through your own
provider keys, with account-scoped memory and versioned task continuity.
Pair a machine only when you want AI tools to reach its files and shell.

## What each step unlocks

Nothing here is all-or-nothing. Steps 1-3 are enough for chat, memory, and
continuity. Step 4 is optional pairing for remote tools.

| Step | What you do | What it unlocks |
|------|-------------|-----------------|
| 1 | Create an account | Dashboard and personal workspace |
| 2 | Connect a provider | Models available through your credentials |
| 3 | Create an `inv_` API key | OpenAI Chat, Anthropic Messages, OpenAI Responses |
| 4 | Pair a machine (optional) | Remote file, shell, and inspection tools |

## Quick start

### Chat without a machine

1. Create an account at `/register`.
2. Add a provider in Dashboard - Providers (name, public HTTPS base URL,
   default model, your key). Upstream providers must support
   OpenAI-compatible Chat Completions.
3. Use the dashboard chat, or create an `inv_` key for your own client.

### Full remote access

```bash
pip install invincible-ai
invincible harness setup     # pair once, prints MCP config
invincible harness connect   # this machine goes online
```

Then authorize an MCP client against `/mcp` (OAuth 2.1 + PKCE in the
browser). Memory tools work immediately; machine tools appear once a
paired machine is connected.

## Key facts

- **No shared provider pool.** Every request routes only through your own
  connected credentials, encrypted at rest.
- **Lexical memory only.** Full-text relevance x recency x kind x
  confidence. Semantic/vector search is not implemented.
- **Continuity is separate from memory.** Versioned task state,
  immutable checkpoints, one pre-failover snapshot.
- **Zero inbound ports.** The paired agent dials out; confirmed actions
  run in a home-relative sandbox behind single-use confirmation tokens.
"""

_INSTALL = """\
# Install the CLI

One package, two commands (`invincible` and `inv` are identical).
Python 3.10+.

## Install

```bash
pip install invincible-ai
```

## Verify

```bash
invincible --version
invincible harness connect --help
```

## What the CLI is for

- `invincible harness setup` — pair this machine once, prints the MCP
  connector config for your AI client.
- `invincible harness connect` — keep this machine online (Ctrl+C to
  stop). `invincible connect` is a short spelling of the same command.
- `invincible harness status` — show pairing and machine state.
- `invincible harness service install` — always-on background service
  (see System Service).

Pairing is optional. Chat, memory, and continuity need no CLI at all.
"""

_SETUP = """\
# Setup guide — from install to AI-ready

## 1. Create an account

Register at `/register` (email + password, or GitHub when enabled).
Your account owns its projects, API keys, provider credentials,
conversations, memory, and continuity state.

## 2. Connect a provider

Dashboard - Providers: add a name, public HTTPS base URL, default model,
and your key. Credentials are encrypted at rest and never sent to your
AI client. With no credentials connected, chat answers 400 and tells you
to connect one.

## 3. Choose how to connect

- **Web chat:** use the dashboard directly.
- **Your own client:** create an `inv_` API key (Dashboard - Account -
  API keys). The raw key is shown once, stored as a hash, revocable.
  Point any OpenAI-compatible client at `/v1/chat/completions`, Claude
  Code at `/v1/messages` via `ANTHROPIC_BASE_URL`, Codex at
  `/v1/responses`.

## 4. Pair a machine (optional)

```bash
invincible harness setup
invincible harness connect
```

The agent connects outbound — your machine never opens a port.
Hosted deployments must enable agent routing.
"""

_CLI = """\
# CLI reference

## Pairing and connection

| Command | What it does |
|---------|--------------|
| `invincible harness setup` | Pair this machine once, print MCP config (idempotent) |
| `invincible harness connect` | Keep this machine online |
| `invincible connect` | Short spelling of `harness connect` |
| `invincible harness status` | Show pairing and machine state |
| `invincible harness service install` | Install the always-on background service |

## Useful management commands

| Command | What it does |
|---------|--------------|
| `invincible login` | Device-flow browser login |
| `invincible doctor` | Check server health, schema, and config |
| `invincible db upgrade` | Run packaged Alembic migrations (never auto-run) |
| `invincible secret credential-key` | Generate `INVINCIBLE_CREDENTIAL_KEY` |

Run any command with `--help` for exact flags. First-time pairing lives
under `harness setup`; `connect` never re-pairs silently.
"""

_MCP = """\
# MCP configuration for AI agents

Point any MCP-compatible client (Cursor, Claude Desktop, Codex, any
custom client) at one endpoint for memory and machine tools.

## Your MCP endpoint

```json
{
  "mcpServers": {
    "invincible": {
      "url": "https://invincible-ai.me/mcp"
    }
  }
}
```

Replace the host with your own deployment when self-hosting.

## Which tools you get

**Memory tools** are available as soon as you connect — no machine
required. **Machine tools** (files, shell, inspection) appear once a
paired machine is online (`invincible harness connect`).

## Authentication

OAuth 2.1 + PKCE in the browser. Approve the connector, get a bearer
token (hashed at rest, revocable). Destructive calls return a
single-use confirmation token first — nothing runs until you approve it.

## Available MCP tools

{{MCP_TOOLS}}

## Security

- Outbound-only agent connection; zero inbound ports.
- Denylist + staged approvals + single-use confirmation tokens.
- Home-relative sandbox on the paired machine.
- Per-user routing: tools act on your machine under your account only.
"""

_MEMORY = """\
# Memory

Account- and project-scoped memories in PostgreSQL: notes, facts,
preferences, decisions — ranked by full-text relevance, recency, kind,
and confidence under one bounded context budget shared with the
continuity brief.

## Saving

Say "remember this" (or "save this") to store explicitly. Manage,
search, and delete everything from Dashboard - Memories.

## Recalling

Retrieval is lexical (Postgres FTS x recency half-life x kind weight x
confidence), AND-then-OR fallback, floor + top-N. Semantic/vector
search is not implemented — a designed but deferred seam.

## Scope

Memories belong to your account (and optionally one project). One
account can never read another's — a foreign id reads exactly like an
unknown one.
"""

_MODELS = """\
# Bring your own models

Invincible holds no provider keys of its own. Add yours in
Dashboard - Providers and use them everywhere.

## Supported upstreams

Anthropic, OpenAI, OpenRouter, Groq, or any OpenAI-compatible Chat
Completions endpoint over public HTTPS. Provider credentials are
encrypted at rest (Fernet, `INVINCIBLE_CREDENTIAL_KEY`) and resolved
at request time — never sent to your AI client.

## Routing modes

Per-user setting: `auto`, `pinned`, or `chain`, with optional order
and per-provider overrides.

## Failover semantics

- 429 / 5xx / network failure: record the failure, cool that credential
  down (30s, doubling to a 300s cap), try your next credential. A
  pre-failover continuity snapshot is captured when task state exists.
- 401 / 403: disable that credential for the server's life, move on.
- All exhausted: answer 503 — your agent retries one stable endpoint.

## Client endpoints

- `POST /v1/chat/completions` — OpenAI Chat Completions + SSE.
- `POST /v1/messages` — Anthropic Messages (Claude Code via
  `ANTHROPIC_BASE_URL`).
- `POST /v1/responses` — OpenAI Responses (Codex CLI).
"""

_AGENT = """\
# Paired agent — hands on your machine

The agent runs on your computer, dials out to the service, and executes
confirmed machine jobs inside a home-relative sandbox.

## Bring it online

```bash
invincible harness setup
invincible harness connect
```

Offline agents answer immediately instead of hanging; reconnects
re-register on the next poll.

## What runs where

On a hosted deployment, approved machine calls run on your paired
machine. On a single-user self-host without agent routing, they run on
the server host itself. Either way the server keeps every security
decision (denylist, staging, single-use confirmation token, audit,
per-user routing) — the agent only does the work.

## Safety

Shell execution and file writes require confirmation. Credential paths
are denylisted and re-checked locally. File reads cap at 64KB with a
truncation flag; search skips secret/state files and large binaries.
"""

_SERVICE = """\
# System service (always-on)

Keep the machine online across reboots without an open terminal.

```bash
invincible harness service install
```

The installer prints exactly what it will register before writing
anything (systemd unit, launchd plist, or Windows service per OS).
Use your platform's service manager to start, stop, or remove it.

Single-instance rule still applies to the server, not the agent:
run one server per database; run one agent per machine you want
reachable.
"""

_SELFHOST = """\
# Self-hosting

The same code runs as a single-user self-host on a laptop backed by
PostgreSQL (local mode is supported indefinitely).

## Prerequisites

- PostgreSQL 17 reachable, e.g.
  `postgresql+asyncpg://invincible@127.0.0.1:5433/invincible`.
  Provision locally with `invincible dev-db` or `docker compose up db`.
- `INVINCIBLE_OWNER_SECRET` set (account sessions fail closed without
  it), `INVINCIBLE_CREDENTIAL_KEY` set (BYOK refuses to run without it).

## Migrate

```bash
invincible db upgrade
```

Migrations run only via this command — never auto-run at startup.
Startup warns and `doctor` fails loudly on stale schemas.

## Run

Single instance (cooldowns, staged approvals, and the agent registry
are in-process). Bind `0.0.0.0:$PORT` behind TLS, forward proxy
headers, expose `/health` for healthchecks. See Deployment for the
two-role database model and go-live checklist.
"""

_ARCH = """\
# Architecture

## Three realms, separate by design

| Surface | Credential | Protects |
|---------|-----------|----------|
| Chat (`/v1/*`) | Per-user `inv_` key (hashed) | BYOK failover routing |
| Tools (`/mcp`) | OAuth bearer (hashed, revocable) | 19 MCP tools + confirmations |
| Account (`/auth/*`) | Signed session cookie | Projects, keys, state |

Realms never merge and never fail open.

## Request paths

- **Chat:** AI client -`inv_ key`-> gateway (auto/pinned/chain over
  your credentials) -`your order`-> your providers.
- **Tools:** MCP client -`Bearer`-> `/mcp` (denylist + confirmation)
  -`agent routing`-> paired machine (home-relative sandbox).
- **State:** account -`owner scope`-> PostgreSQL (sessions, memory,
  tasks) -`projections`-> dashboard (usage, history).

## Single failover loop

Exactly one attempt loop exists (`router._iter_attempts`);
`route_request`/`stream_open` are thin wrappers. Run recording writes
one `runs` row per upstream attempt; the dashboard aggregates
attempts, failovers, and tokens by day and provider/model.
"""

# slug, title, group, description, body
PAGES: dict = {
    "introduction": {
        "title": "Introduction",
        "group": "Getting Started",
        "desc": "What Invincible is and what each step unlocks.",
        "body": _INTRO,
    },
    "installation": {
        "title": "Install the CLI",
        "group": "Getting Started",
        "desc": "One package on the machine you want to reach.",
        "body": _INSTALL,
    },
    "setup": {
        "title": "Setup guide",
        "group": "Getting Started",
        "desc": "From install to AI-ready.",
        "body": _SETUP,
    },
    "cli": {
        "title": "CLI reference",
        "group": "Remote",
        "desc": "Pair, connect, and manage machines.",
        "body": _CLI,
    },
    "mcp": {
        "title": "MCP configuration",
        "group": "Remote",
        "desc": "Connect Cursor, Claude, Codex, or any MCP client.",
        "body": _MCP,
    },
    "memory": {
        "title": "Memory",
        "group": "Memory, Models & Agent",
        "desc": "Durable context with no install.",
        "body": _MEMORY,
    },
    "models": {
        "title": "Bring your own models",
        "group": "Memory, Models & Agent",
        "desc": "Anthropic, OpenAI, OpenRouter, and more.",
        "body": _MODELS,
    },
    "agent": {
        "title": "Paired agent",
        "group": "Memory, Models & Agent",
        "desc": "Hands on your machine, outbound-only.",
        "body": _AGENT,
    },
    "service": {
        "title": "System service",
        "group": "Advanced",
        "desc": "Always-on background agent.",
        "body": _SERVICE,
    },
    "self-hosting": {
        "title": "Self-hosting",
        "group": "Advanced",
        "desc": "Run the same code on your laptop.",
        "body": _SELFHOST,
    },
    "architecture": {
        "title": "Architecture",
        "group": "Advanced",
        "desc": "Realms, request paths, failover.",
        "body": _ARCH,
    },
}

ORDER = ["introduction", "installation", "setup", "cli", "mcp", "memory",
         "models", "agent", "service", "self-hosting", "architecture"]

GROUPS = ["Getting Started", "Remote", "Memory, Models & Agent", "Advanced"]


def _render_body(body: str) -> str:
    if "{{MCP_TOOLS}}" in body:
        lines = ["| Tool | What it does |", "|------|----------------|"]
        for t in _mcp_tools():
            lines.append(f"| `{t['name']}` | {t['desc']} |")
        body = body.replace("{{MCP_TOOLS}}", "\n".join(lines))
    return _md().render(body)


def _sidebar() -> list:
    groups = []
    for g in GROUPS:
        items = [{"slug": s, "title": PAGES[s]["title"]}
                 for s in ORDER if PAGES[s]["group"] == g]
        groups.append({"name": g, "items": items})
    return groups


def _page_context(slug: str) -> dict:
    idx = ORDER.index(slug)
    prev_slug = ORDER[idx - 1] if idx > 0 else None
    next_slug = ORDER[idx + 1] if idx + 1 < len(ORDER) else None
    body = PAGES[slug]["body"]
    return {
        "slug": slug,
        "title": PAGES[slug]["title"],
        "desc": PAGES[slug]["desc"],
        "content_html": _render_body(body),
        "toc": _toc(body),
        "sidebar": _sidebar(),
        "prev": ({"slug": prev_slug, "title": PAGES[prev_slug]["title"]}
                 if prev_slug else None),
        "next": ({"slug": next_slug, "title": PAGES[next_slug]["title"]}
                 if next_slug else None),
    }


@router.get("/docs", include_in_schema=False)
async def docs_index(request: Request):
    ctx = _page_context("introduction")
    ctx["is_index"] = True
    return templates.TemplateResponse(request, "docs.html", ctx)


@router.get("/docs/{slug}", include_in_schema=False)
async def docs_page(request: Request, slug: str):
    if slug not in PAGES:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "Unknown docs page"}, status_code=404)
    ctx = _page_context(slug)
    ctx["is_index"] = slug == "introduction"
    return templates.TemplateResponse(request, "docs.html", ctx)
