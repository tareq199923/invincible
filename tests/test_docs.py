# tests/test_docs.py
"""Public docs site (/docs): curated guides, allowlist only.

Hermetic by design: the docs routes touch no stores and need no auth,
so these tests drive the real app over ASGI without the Postgres-backed
``client`` fixture (same category as the router/selection hermetic
suites in TESTING.md).
"""
import json
import re

import httpx
import pytest

from invincible.main import app

SLUGS = ["introduction", "installation", "setup", "cli", "mcp", "memory",
         "models", "agent", "service", "self-hosting", "architecture"]


@pytest.fixture
async def docs_client():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def test_docs_index_renders(docs_client):
    resp = await docs_client.get("/docs")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    # Swagger must not own /docs anymore (docs_url=None in main.py).
    assert "swagger" not in body.lower()
    assert "Introduction to Invincible" in body
    assert "On this page" in body
    for slug in SLUGS:
        assert f'href="/docs/{slug}"' in body


async def test_docs_slugs_render(docs_client):
    for slug in SLUGS:
        resp = await docs_client.get(f"/docs/{slug}")
        assert resp.status_code == 200, slug
        assert "text/html" in resp.headers["content-type"]
    body = (await docs_client.get("/docs/mcp")).text
    # MCP tool table is generated from the live TOOLS list (21 tools).
    for tool in ("read_file", "execute_bash", "write_file", "edit_file",
                 "confirm_action", "memory_save", "memory_search"):
        assert tool in body
    assert body.count("<tr>") >= 20
    cli = (await docs_client.get("/docs/cli")).text
    assert "harness setup" in cli
    assert "harness connect" in cli
    mem = (await docs_client.get("/docs/memory")).text
    assert "Semantic/vector" in mem
    assert "not implemented" in mem


async def test_docs_honesty_markers(docs_client):
    intro = (await docs_client.get("/docs")).text
    assert "No shared provider pool" in intro
    assert "Lexical memory only" in intro
    models = (await docs_client.get("/docs/models")).text
    assert "401 / 403" in models
    assert "503" in models
    arch = (await docs_client.get("/docs/architecture")).text
    assert "inv_" in arch
    assert "OAuth" in arch


async def test_docs_unknown_and_internal_404(docs_client):
    for slug in ("nope", "multi-tenant-audit",
                 "workqueue", "railway-account-transfer", "testing",
                 "releasing"):
        resp = await docs_client.get(f"/docs/{slug}")
        assert resp.status_code == 404, slug


async def test_docs_no_secrets_leak(docs_client):
    # Env var NAMES are public config (same as CONFIGURATION.md); what
    # must never render is a value: provider keys, Bearer tokens, raw
    # API keys, or private-key material.
    pages = [(await docs_client.get(f"/docs/{s}")).text for s in SLUGS]
    blob = "\n".join(pages)
    assert "sk-ant-" not in blob
    assert "-----BEGIN" not in blob
    # No Bearer tokens rendered.
    assert not re.search(r"Bearer\s+[A-Za-z0-9_\-]{16,}", blob)
    # No env-var assignments with values.
    assert not re.search(r"INVINCIBLE_\w+\s*=\s*['\"]?\w", blob)


async def test_landing_links_docs(docs_client):
    body = (await docs_client.get(
        "/", headers={"Accept": "text/html"})).text
    assert 'href="/docs"' in body


# ---------------------------------------------------------------------------
# Parity pass: request-derived snippets, docs chrome, copy buttons, search.
# ---------------------------------------------------------------------------

# The nine tools whose dispatch branches in endpoints/mcp.py touch only
# the caller's own rows (memory, task state, todos, projects): they work
# with no machine online, so they must render in the first table.
DATA_PLANE_TOOLS = ("memory_save", "memory_search", "memory_list",
                    "task_state_set", "task_state_get", "checkpoint_create",
                    "todo",
                    "project_create", "project_list")


async def test_docs_snippets_use_request_host(docs_client):
    """A self-host documents its own domain: no docs page may bake in the
    production host (the landing page pinned the same rule)."""
    pages = [(await docs_client.get(f"/docs/{s}")).text for s in SLUGS]
    mcp = (await docs_client.get("/docs/mcp")).text
    assert "http://test/mcp" in mcp
    assert "{{BASE_URL}}" not in mcp
    assert "{{MCP_TOOLS}}" not in mcp
    assert "invincible-ai.me" not in "\n".join(pages)


async def test_docs_chrome_markers(docs_client):
    body = (await docs_client.get("/docs/memory")).text
    assert 'rel="canonical" href="http://test/docs/memory"' in body
    assert 'property="og:url" content="http://test/docs/memory"' in body
    assert 'property="og:site_name" content="Invincible"' in body
    assert 'name="twitter:card" content="summary"' in body
    assert "/static/favicon.svg" in body
    assert "docs.js?v=" in body
    # The index page canonicalizes to /docs, and the header carries the
    # Dashboard link (flexx-parity nav: Home - Dashboard - search).
    index = (await docs_client.get("/docs")).text
    assert 'rel="canonical" href="http://test/docs"' in index
    assert 'href="/dashboard"' in index
    assert 'id="palette-open"' in index


async def test_docs_heading_ids_and_search_index(docs_client):
    body = (await docs_client.get("/docs/memory")).text
    # Anchors are injected server-side, so the TOC link target exists
    # without any client-side rewriting.
    assert '<h2 id="saving">' in body
    assert 'href="#saving"' in body
    match = re.search(
        r'<script type="application/json" id="docs-search-index">(.*?)'
        r"</script>", body, re.S)
    assert match, "search index payload missing"
    index = json.loads(match.group(1))
    assert [page["slug"] for page in index] == SLUGS
    for page in index:
        assert page["title"] and page["desc"]
        assert page["headings"], page["slug"]
        assert all(h["anchor"] and h["title"] for h in page["headings"])
    memory = next(p for p in index if p["slug"] == "memory")
    assert "saving" in [h["anchor"] for h in memory["headings"]]


async def test_docs_copy_hooks_and_palette_markup(docs_client):
    body = (await docs_client.get("/docs/installation")).text
    # Code blocks render plain; docs.js wraps them and injects the
    # button, announcing the outcome through the live region.
    assert "<pre>" in body
    assert 'id="copy-status"' in body
    assert 'role="status"' in body
    assert 'id="docs-palette"' in body
    assert 'id="palette-results"' in body
    assert 'role="combobox"' in body
    # No inline script left in the template (all behavior is vendored).
    assert "<script>" not in body


async def test_docs_mcp_tool_planes(docs_client):
    """The live tool table is split by where the tool actually runs, and
    every shipped tool appears exactly once."""
    from invincible.endpoints.mcp import TOOLS

    body = (await docs_client.get("/docs/mcp")).text
    assert "Available with no machine online" in body
    first, _, second = body.partition("once a paired machine is online:")
    assert second, "machine-plane table caption missing"
    for name in DATA_PLANE_TOOLS:
        assert f"<code>{name}</code>" in first, name
        assert f"<code>{name}</code>" not in second, name
    for name in ("read_file", "execute_bash", "write_file", "code_search",
                 "list_dir", "find_files", "git_status", "process_list",
                 "screenshot", "web_fetch"):
        assert f"<code>{name}</code>" in second, name
    for tool in TOOLS:
        assert body.count(f"<code>{tool['name']}</code>") == 1, tool["name"]


async def test_docs_toc_anchors_match_injected_ids(docs_client):
    """The sidebar TOC links resolve to the ids the server injects: the
    palette, the TOC, and the rendered headings share one slug rule."""
    for slug in SLUGS:
        url = "/docs" if slug == "introduction" else f"/docs/{slug}"
        body = (await docs_client.get(url)).text
        ids = re.findall(r'<h[23] id="([^"]+)"', body)
        toc = re.search(r'class="toc".*?</aside>', body, re.S)
        assert toc, slug
        anchors = re.findall(r'href="#([^"]+)"', toc.group(0))
        assert ids == anchors, slug
        assert ids, slug


async def test_docs_cli_reference_matches_shipped_surface(docs_client):
    """Every command family the CLI exposes is documented, and the two
    pairing commands the tree pins elsewhere stay present."""
    body = (await docs_client.get("/docs/cli")).text
    for command in ("harness setup", "harness connect", "harness status",
                    "harness service install", "login", "setup", "start",
                    "doctor", "db upgrade", "secret rotate",
                    "secret credential-key", "dev-db", "update",
                    "users list", "users reset-password", "api-key create",
                    "api-key list", "api-key revoke", "oauth list",
                    "oauth revoke"):
        assert f"<code>{command}</code>" in body, command

