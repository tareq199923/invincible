# invincible/endpoints/docs.py
"""Public documentation site.

Curated, standalone pages served by the app itself: ``GET /docs`` renders
the introduction, ``GET /docs/{slug}`` renders one allowlisted guide.
Unknown slugs 404 — internal repo docs (audits, account transfers,
workqueues, gap analyses) are never published here.

Content discipline (AGENTS.md): docs follow implementation. CLI commands
mirror ``cli.py`` (``harness setup`` / ``harness connect``), the MCP tool
table is generated from the live ``TOOLS`` list in ``endpoints/mcp.py``,
and every behavior claim (BYOK-only, lexical memory, failover codes) matches
the tested semantics. No secrets are rendered.

Two presentation invariants are test-pinned: page bodies never hardcode a
host (``{{BASE_URL}}`` is the request's own base URL, so a self-host
documents its own domain), and heading anchors are injected server-side
from the same ``_slugify`` the sidebar TOC and the ``⌘K`` search index use,
so the three can never disagree.
"""
import re
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

from invincible.core import harness_tools
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


# Plane split for the docs tool tables. Data-plane tools read/write the
# caller's own rows (memory, task state, projects) and need no machine
# online; every other tool executes on a paired machine. Membership is
# derived from the tool registry (same data-plane flag the dispatch
# branches implement), and tests/test_docs.py pins it against the live
# TOOLS list so a rename fails loudly instead of silently misfiling a
# tool in the docs.
_DATA_PLANE_TOOLS = harness_tools.docs_data_plane_names()


def _mcp_tool_rows() -> dict:
    """Live tool names + first-sentence descriptions, split by plane."""
    from invincible.endpoints.mcp import TOOLS
    groups: dict = {"data": [], "machine": []}
    for t in TOOLS:
        desc = (t.get("description") or "").split(".")[0].strip() + "."
        plane = "data" if t["name"] in _DATA_PLANE_TOOLS else "machine"
        groups[plane].append({"name": t["name"], "desc": desc})
    return groups


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

Two names, identical entry points: `invincible` and `inv`. The tables
below omit the `invincible` prefix, and every row is verified against
`--help` on the shipped package.

## Pair and connect

| Command | What it does |
|---------|--------------|
| `harness setup` | Pair once, print the MCP connector config |
| `harness connect` | Keep this machine online (Ctrl+C to stop) |
| `connect` | Short spelling of `harness connect` |
| `harness status` | Account, agent liveness, machine inventory |
| `harness service install` | Write the always-on service (`--dry-run` prints) |
| `login` | Device-flow pairing; `--server` for a self-host |

## Server administration (self-host)

| Command | What it does |
|---------|--------------|
| `setup` | Create or update `.env` (secrets never echoed) |
| `start` | Start the gateway: `--host`, `--port`, `--tunnel` |
| `doctor` | Environment and config diagnostics; schema check |
| `db upgrade` | Run the packaged migrations to head (never auto) |
| `secret rotate` | New `INVINCIBLE_OWNER_SECRET`, written in place |
| `secret credential-key` | Generate the BYOK Fernet key |
| `dev-db` | Local development Postgres; prints the DSN |
| `update` | Install the latest `invincible-ai` from PyPI |

## Accounts, keys, and grants

| Command | What it does |
|---------|--------------|
| `users list` | List dashboard accounts (host tool) |
| `users reset-password` | Password reset — host recovery path |
| `api-key create` | Mint an `inv_` key (raw value shown once) |
| `api-key list` | List keys by label and prefix |
| `api-key revoke` | Revoke a key by id or visible prefix |
| `oauth list` | OAuth clients and their active MCP grants |
| `oauth revoke` | Revoke every token issued to one client |

Run any command with `--help` for the exact flags. First-time pairing
lives under `harness setup`; `connect` never re-pairs silently.
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
      "url": "{{BASE_URL}}/mcp"
    }
  }
}
```

The host above is derived from the request you are reading this on — a
self-hosted deployment documents its own domain, never someone else's.
Paste it into your client exactly as written.

## Supported AI tools

Any MCP-compatible client works: the endpoint is standard Model Context
Protocol with OAuth. These are the paths that have been exercised.

| Client | Where the server goes |
|--------|-----------------------|
| Cursor | Settings - MCP: add the URL above |
| Claude Desktop | `mcpServers` in `claude_desktop_config.json` |
| ChatGPT | add the MCP endpoint as a connector |
| OpenAI Codex | standard MCP config with the URL above |
| Any MCP client | the JSON above; `url` is required |

## Which tools you get

**Memory, continuity, and project tools** work as soon as you connect.
**Machine tools** appear once a paired machine is online
(`invincible harness connect`) — the two tables at the end of this page
list exactly which is which.

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

Say "remember this" (or "save this") to store explicitly. A deterministic
extractor also records durable preferences and decisions from your own
turns — never from assistant replies or tool results, so a fetched page
or a provider reply can never mint one.

The MCP tools `memory_save`, `memory_search`, and `memory_list` write and
read the same rows (confidence 0.9, provenance `mcp:<client>`). They are
data-plane tools: no confirmation gate, because they only ever touch the
caller's own memory — and there is no MCP delete.

## Recalling

Retrieval is lexical (Postgres FTS x recency half-life x kind weight x
confidence), AND-then-OR fallback, floor + top-N. Semantic/vector
search is not implemented — a designed but deferred seam.

## Browsing

Dashboard - Memory at `/dashboard/memory` renders the graph view: nodes
for memories and their sources, edges to the project they belong to.
Search, filter by kind, and delete from the same page. The surface is
session-only: it 401s without a cookie, and an `inv_` key can never
reach it.

## Scope

Memories belong to your account (and optionally one project). One
account can never read another's — a foreign id reads exactly like an
unknown one.

## Memory is not continuity

Memory is durable context. Continuity is versioned task state
(`task_state_set`, `task_state_get`, `checkpoint_create`) with immutable
checkpoints and one pre-failover snapshot. They share a single bounded
context budget and nothing else.
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

## Install

```bash
invincible harness service install
```

The installer prints exactly what it will register before writing
anything (systemd unit, launchd plist, or Windows service per OS).
Use your platform's service manager to start, stop, or remove it.

## One server, many agents

The single-instance rule applies to the server, not the agent: run one
server per database, and one agent per machine you want reachable.
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


_HEADING_RE = re.compile(r"<(h[23])>(.*?)</\1>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _with_heading_ids(html: str) -> str:
    """Give rendered h2/h3 elements the ids the sidebar TOC and the search
    palette link to (``_slugify`` of the heading text). Doing it here
    keeps one source of anchor truth: inline markup (``code``, ``em``)
    disappears the same way in both places, so no client-side rewrite can
    drift from the table of contents."""
    def repl(match: re.Match) -> str:
        tag, inner = match.group(1), match.group(2)
        text = _TAG_RE.sub("", inner)
        return f'<{tag} id="{_slugify(text)}">{inner}</{tag}>'
    return _HEADING_RE.sub(repl, html)


def _tool_table(rows: list) -> list:
    lines = ["| Tool | What it does |", "|------|----------------|"]
    for t in rows:
        lines.append(f"| `{t['name']}` | {t['desc']} |")
    return lines


def _render_body(body: str, base_url: str = "") -> str:
    if "{{MCP_TOOLS}}" in body:
        groups = _mcp_tool_rows()
        lines = ["**Available with no machine online** — your own rows:",
                 ""]
        lines += _tool_table(groups["data"])
        lines += ["", "**Machine tools** — once a paired machine is online:",
                  ""]
        lines += _tool_table(groups["machine"])
        body = body.replace("{{MCP_TOOLS}}", "\n".join(lines))
    if "{{BASE_URL}}" in body:
        # Request-derived host, same rule as the landing page: a
        # self-hosted copy must never print the production domain.
        body = body.replace("{{BASE_URL}}", base_url or "")
    return _with_heading_ids(_md().render(body))


def _sidebar() -> list:
    groups = []
    for g in GROUPS:
        items = [{"slug": s, "title": PAGES[s]["title"]}
                 for s in ORDER if PAGES[s]["group"] == g]
        groups.append({"name": g, "items": items})
    return groups


def _search_index() -> list:
    """Every page with its h2/h3 anchors, for the client-side palette.

    Built from ``_toc`` (a regex pass over the curated markdown), so the
    index costs no markdown rendering and cannot disagree with the
    injected heading ids.
    """
    index = []
    for slug in ORDER:
        headings = [{"title": h["title"], "anchor": h["anchor"]}
                    for h in _toc(PAGES[slug]["body"])]
        index.append({
            "slug": slug,
            "title": PAGES[slug]["title"],
            "desc": PAGES[slug]["desc"],
            "headings": headings,
        })
    return index


def _base_url(request: Request) -> str:
    """The host this request arrived on — same convention as the landing
    page and the OAuth metadata."""
    return str(request.base_url).rstrip("/")


def _page_context(slug: str, base_url: str = "") -> dict:
    idx = ORDER.index(slug)
    prev_slug = ORDER[idx - 1] if idx > 0 else None
    next_slug = ORDER[idx + 1] if idx + 1 < len(ORDER) else None
    body = PAGES[slug]["body"]
    canonical = (f"{base_url}/docs" if slug == "introduction"
                 else f"{base_url}/docs/{slug}")
    return {
        "slug": slug,
        "title": PAGES[slug]["title"],
        "desc": PAGES[slug]["desc"],
        "content_html": _render_body(body, base_url),
        "toc": _toc(body),
        "sidebar": _sidebar(),
        "search_index": _search_index(),
        "base_url": base_url,
        "canonical_url": canonical,
        "prev": ({"slug": prev_slug, "title": PAGES[prev_slug]["title"]}
                 if prev_slug else None),
        "next": ({"slug": next_slug, "title": PAGES[next_slug]["title"]}
                 if next_slug else None),
    }


@router.get("/docs", include_in_schema=False)
async def docs_index(request: Request):
    ctx = _page_context("introduction", _base_url(request))
    ctx["is_index"] = True
    return templates.TemplateResponse(request, "docs.html", ctx)


@router.get("/docs/{slug}", include_in_schema=False)
async def docs_page(request: Request, slug: str):
    if slug not in PAGES:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "Unknown docs page"}, status_code=404)
    ctx = _page_context(slug, _base_url(request))
    ctx["is_index"] = slug == "introduction"
    return templates.TemplateResponse(request, "docs.html", ctx)

