# tests/test_docs.py
"""Public docs site (/docs): curated flexx-style guides, allowlist only.

Hermetic by design: the docs routes touch no stores and need no auth,
so these tests drive the real app over ASGI without the Postgres-backed
``client`` fixture (same category as the router/selection hermetic
suites in TESTING.md).
"""
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
    # MCP tool table is generated from the live TOOLS list (19 tools).
    for tool in ("read_file", "execute_bash", "write_file",
                 "confirm_action", "memory_save", "memory_search"):
        assert tool in body
    assert body.count("<tr>") >= 19
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
    for slug in ("nope", "multi-tenant-audit", "invincible-vs-flexx",
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
