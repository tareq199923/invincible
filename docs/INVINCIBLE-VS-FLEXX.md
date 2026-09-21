# Invincible vs. flexx.dev — Comparison & Gap Analysis

**Date:** 2026-09-17

**Status re-checked 2026-09-18:** Gap items — semantic memory retrieval, tool auto-discovery, and localhost tunneling — remain unstarted (distinct from the existing Cloudflare gateway tunnel).

---

## 1. What is flexx.dev

flexx.dev is a hosted SaaS built around four connected products:

- **Flexx Remote** — a Go binary daemon installed on a user's machine that opens an *outbound-only* WebSocket connection to a relay (`relay.flexx.dev`). AI agents (Cursor, Claude, ChatGPT, or any MCP client) connect to the relay and get remote hands on that machine — zero inbound ports required.
- **Flexx Router** — a bring-your-own-key (BYOK) model gateway. Users add Anthropic/OpenAI/OpenRouter/Groq/any OpenAI-compatible key in a dashboard; Router exposes one OpenAI-compatible (`/v1/chat/completions`) and Anthropic-compatible (`/v1/messages`) endpoint with per-key budgets and allowlists.
- **Flexx Memory** — a graph-backed, client-agnostic memory system over MCP. Works with zero install (no machine required) — any MCP client can save/recall decisions, preferences, and project context. Uses semantic search, not just lexical.
- **Flexx Agent** — a unified assistant that already has Remote + Router + Memory wired together; effectively their own coding harness (comparable to Claude Code / Cursor), built natively for the web.
- **Flexx Artifact** — mentioned by the developer in a public post as part of "the latest iteration," but **not documented anywhere** — not on the flexx.dev landing page, not in their FAQ/pricing, no GitHub hits, no docs page reference. Likely unshipped, internal, or unannounced. Unconfirmed.

### Origin story (from the developer's own post)
Started because his Cursor subscription lapsed and he wanted to use AI assistants from his phone against his home PC. Built a relay daemon (Remote) → wanted shared memory across AI tools (Memory) → needed one place for model keys (Router) → ended up with enough pieces to accidentally build his own coding harness (Agent). Notable parallel: this mirrors Invincible's own growth from a single-user tool into a multi-user continuity platform — organic, need-driven, shipped incrementally rather than planned upfront.

### Pricing / packaging (live)
- **Open Source** — $0 forever, self-hosted relay & daemon, unlimited MCP tools, localhost tunneling, community support.
- **Cloud Free** — $0 forever, hosted relay, zero-config OAuth, unlimited remotes, knowledge graph memory, encrypted reverse tunnels, audit logs & vision.
- **Team** — custom pricing, multi-user org access, custom tunnel domains, SSO, dedicated support.

---

## 2. What is Invincible (your project)

A remote-first, multi-user AI continuity platform. Core principle: *"The LLM is replaceable. The user's identity, projects, memory, and continuity are not."*

Key pieces already shipped (per ROADMAP.md, all marked Implemented/Complete as of the last audit):
- **Gateway**: OpenAI + Anthropic compatible endpoints, SSE streaming, unified failover routing across providers.
- **Identity & isolation**: users/projects/api_keys/audit_log schema, OAuth 2.1 + PKCE, GitHub login, ownership predicates verified via a full multi-tenant security audit (closed 2026-09-07, all HIGH/MEDIUM/LOW findings fixed and deployed).
- **Continuity Engine**: versioned `task_states`, immutable checkpoints, reactive failover snapshots, interruption detection — a dedicated system separate from memory.
- **Memory**: scoped `memories` table, lexical retrieval only (Postgres FTS × recency half-life × kind weight × confidence). Semantic/vector retrieval is a **designed but deferred** seam (`RetrievalService`/`EmbeddingProvider`).
- **MCP tools**: `read_file`, `write_file`, `execute_bash`, `confirm_action` (staged approval + denylist), plus continuity and memory tools.
- **Phase 10 — Local Agent**: moves confirmed tool execution off the server and onto the user's own machine via long-poll (`POST /agent/poll` / `POST /agent/result`), zero inbound ports, home-relative sandbox, local denylist re-check.
- **BYOK provider connections (Phase 9, complete)**: encrypted credential storage (Fernet), connect/list/test/remove API, SSRF guard, per-user router candidate pool, dashboard Providers UI.
- **Dashboard**: Jinja2 + HTMX — sessions, tasks, memory browse/search, memory graph (visual), usage, settings.
- **Deployment**: live on Railway + Neon Postgres at `invincible-ai.me`; Railway trial ends ~2026-10-02, migration to Azure for Students planned.

Not yet productized: no public sign-up funnel, no pricing tiers, single hosted instance you manage directly rather than a self-serve SaaS.

---

## 3. Feature-by-feature comparison

| Capability | flexx.dev | Invincible |
|---|---|---|
| Remote machine execution | Go binary, outbound WebSocket to relay, zero inbound ports | Phase 10 local agent: long-poll HTTPS, also zero inbound ports — different transport, same guarantee |
| SSH access | Native (`ssh flexx-myserver`, auto ProxyCommand) | **Not built** |
| Localhost tunneling | Built-in, one command | **Not built** |
| Screenshots / vision | Built-in (Chrome-based) | **Not built** |
| Tool auto-discovery | ripgrep, Docker, Jupyter, Chrome, GPUs detected automatically | **Not built** |
| Memory | Graph-backed, semantic search, zero-install | Lexical only (Postgres FTS); semantic/vector retrieval **deferred**, not built |
| Continuity (task state) | Not described as a separate concept from memory | Dedicated ContinuityEngine — versioned task states, checkpoints, reactive failover snapshots. **More rigorous than flexx's public description.** |
| BYOK model routing | Anthropic/OpenAI/OpenRouter/Groq, OpenAI+Anthropic compatible endpoints, per-key budgets | Phase 9 complete — same idea, with SSRF guarding and audit logging documented |
| Auth | OAuth 2.1 + PKCE | OAuth 2.1 + PKCE (same standard) |
| Sandbox / safety | Path scoping, command blocklist, file size limits | Denylist + staged-action confirm tokens + home-relative sandbox (Phase 10 "wall 2/3") — comparable rigor |
| Multi-tenant isolation hardening | Not publicly documented | Full audit closed 2026-09-07: ownership predicates everywhere, fail-loud fallbacks, cross-user regression test suite. **No public equivalent from flexx.** |
| Self-host option | Yes, free, open source | Yes — "local mode" explicitly preserved indefinitely |
| Unified product surface (coding harness) | Yes — Flexx Agent ties Remote+Router+Memory into one assistant | **Not built** — pieces exist (agent execution, BYOK, memory) but no unified UI/harness on top |
| Pricing/packaging | Live: Open Source ($0), Cloud Free, Team (custom) | **Not productized** — single hosted instance, no tiers, no public sign-up funnel |

---

## 4. What Invincible is missing (gap list, priority order)

1. **Semantic/vector memory retrieval** — highest leverage. Seam already designed (`RetrievalService`/`EmbeddingProvider`), just not implemented. Would close the biggest capability gap with the least new architecture.
2. **Tool auto-discovery** — cheap relative to payoff. Detecting ripgrep/Docker/Jupyter/GPUs on the paired machine and surfacing that to the agent enriches existing tools without a new subsystem.
3. **Localhost tunneling** — natural extension of the already-built Phase 10 outbound-connectivity channel.
4. **Screenshots / vision** — useful for UI/testing workflows; heavier lift (Chrome dependency, image handling through the MCP pipeline).
5. **SSH access** — nice-to-have parity feature, but doesn't map naturally onto the long-poll agent architecture the way it does onto flexx's persistent WebSocket. Bigger architectural lift for comparatively modest benefit — lower priority unless specifically requested.
6. **Productized packaging/pricing tiers** — not a technical feature, but relevant: this is exactly what Invincible's own Phase 6 (Distribution) and the pending WORKQUEUE.md decision (PyPI vs. `.exe`, hosted vs. self-hosted) are already gesturing toward. Worth using flexx's tier boundaries as a reference point.
7. **Unified coding-harness UI ("Agent")** — flexx built this almost by accident once Remote+Router+Memory existed. Invincible has the same underlying substrate; a thin harness UI on top could be a natural next step, but isn't currently queued and would compete for attention with the Azure migration and semantic memory work.

---

## 5. Where Invincible is ahead

- **Continuity engine** — versioned task states, immutable checkpoints, reactive failover snapshots. No public equivalent described by flexx.
- **Multi-tenant isolation rigor** — a full closed-out security audit (ownership predicates, fail-loud fallback elimination, cross-user regression tests) with nothing comparable publicly documented for flexx.
- **BYOK design detail** — roughly at parity functionally, but Invincible's SSRF guarding and audit-log coverage are explicitly documented.

---

## 6. Bottom line

The gap is almost entirely in **breadth of "hands" features** (SSH, tunneling, screenshots, auto-discovery) and **go-to-market polish** (pricing tiers, self-serve sign-up, a unified product surface) — not in core architecture, where Invincible's continuity and isolation work is more rigorous than anything flexx has shown publicly.
