# tests/test_memory_graph.py
"""Memory-graph projection (Level 1 + Level 2 similarity edges).

Covers the projection payload (nodes/edges/timeline/summary/layout),
the cookie-realm gate on both surfaces, cross-user invisibility,
project-union filter semantics, the visible memory-node cap, the
similar_to derivation (shared-keyword edges + keywords field), the
merged-page redirect, and JSON/page payload parity - the data contract
the interactive graph.js renderer consumes unchanged.
"""
import time

from invincible.core.accounts import ProjectService
from invincible.core.memory import MemoryStore
from invincible.core.memory_projection import (
    MEMORY_NODE_CAP,
    build_memory_projection,
    classify_source,
    similar_edges,
    source_color,
)
from tests.conftest import register_account

# --- Source classification (pure) ------------------------------------------


def test_classify_source_collapses_provenance():
    assert classify_source(None) == "dashboard"
    assert classify_source("chat:abc-123") == "chat"
    assert classify_source("mcp:grok") == "mcp:grok"
    assert classify_source("mcp:claude") == "mcp:claude"


def test_source_color_is_stable():
    assert source_color("mcp:grok") == source_color("mcp:grok")
    assert len({source_color(s) for s in
                ("dashboard", "chat", "mcp:grok")}) >= 2


# --- Auth gates -------------------------------------------------------------


async def test_graph_surfaces_require_session(client):
    assert (await client.get("/memories/graph")).status_code == 401
    assert (await client.get(
        "/dashboard/memory/graph")).status_code == 401


async def test_graph_rejects_api_key_realm(client):
    from invincible.endpoints.accounts import SESSION_COOKIE
    from invincible.main import app
    from tests.conftest import promote_operator
    made, _ = await register_account(client, "op@example.com")
    await promote_operator(made.json()["id"])
    from invincible.core.identity import ApiKeyStore
    key = await ApiKeyStore(app.state.engine).create(
        made.json()["id"], label="graph probe")
    client.cookies.delete(SESSION_COOKIE)  # key alone, no session cookie
    resp = await client.get(
        "/memories/graph", headers={"Authorization": f"Bearer {key['raw']}"})
    assert resp.status_code == 401  # inv_ keys never reach the dashboard


# --- Projection shape -------------------------------------------------------


async def _seed(app, user_id, *, count=1, provenance=None,
                project_id=None, kind="note"):
    store = MemoryStore(app.state.engine)
    made = []
    for i in range(count):
        made.append(await store.save_memory(
            user_id=user_id,
            content=f"memory {i} {time.time()}",
            layer="explicit" if provenance is None else "auto",
            kind=kind,
            confidence=1.0 if provenance is None else 0.6,
            provenance=provenance,
            project_id=project_id,
        ))
    return made


async def test_empty_store_yields_valid_empty_projection(client):
    from invincible.main import app

    made, _ = await register_account(client, "empty@example.com")
    uid = made.json()["id"]
    payload = await build_memory_projection(
        MemoryStore(app.state.engine),
        ProjectService(app.state.engine),
        user_id=uid, user_label="empty@example.com",
    )
    assert payload["summary"]["total"] == 0
    assert payload["summary"]["shown"] == 0
    assert not payload["summary"]["truncated"]
    node_kinds = {n["kind"] for n in payload["nodes"]}
    assert node_kinds == {"user"}  # the root, nothing else
    assert payload["edges"] == []
    assert payload["timeline"] == []
    # The merged page still renders for an empty store.
    resp = await client.get("/dashboard/memory")
    assert resp.status_code == 200
    assert "No memories" in resp.text


async def test_multi_project_multi_source_shapes(client):
    from invincible.main import app

    made, _ = await register_account(client, "shaper@example.com")
    uid = made.json()["id"]
    projects = ProjectService(app.state.engine)
    proj = await projects.create(uid, "invincible")
    default = await projects.list(uid)
    default_id = next(p["id"] for p in default if p["is_default"])

    await _seed(app, uid, count=2, provenance="mcp:grok",
                project_id=proj["id"])
    await _seed(app, uid, count=1, provenance="chat:sess-1",
                project_id=default_id)
    await _seed(app, uid, count=1, provenance=None)  # dashboard save

    payload = await build_memory_projection(
        MemoryStore(app.state.engine), projects, user_id=uid)
    kinds = {}
    for node in payload["nodes"]:
        kinds.setdefault(node["kind"], 0)
        kinds[node["kind"]] += 1
    assert kinds == {"user": 1, "memory": 4, "project": 3, "source": 3}
    assert payload["summary"]["total"] == 4
    assert payload["summary"]["by_source"] == {
        "chat": 1, "dashboard": 1, "mcp:grok": 2}
    assert payload["summary"]["by_project"]["invincible"] == 2
    assert payload["summary"]["by_project"]["user-scope"] == 1
    # Every memory has both belongs_to and saved_by edges.
    belongs = [e for e in payload["edges"] if e["kind"] == "belongs_to"]
    saved = [e for e in payload["edges"] if e["kind"] == "saved_by"]
    owned = [e for e in payload["edges"] if e["kind"] == "owned_by"]
    assert len(belongs) == 4 and len(saved) == 4 and len(owned) == 3
    # Timeline is oldest-first.
    times = [n["ts"] for n in payload["nodes"]
             if n["kind"] == "memory"]
    timeline_ts = [
        next(n["ts"] for n in payload["nodes"] if n["id"] == node_id)
        for node_id in payload["timeline"]
    ]
    assert timeline_ts == sorted(times)
    # Layout covers every node.
    assert set(payload["layout"]) == {n["id"] for n in payload["nodes"]}
    # Memory dots carry their source color.
    mem = next(n for n in payload["nodes"] if n["kind"] == "memory")
    assert payload["layout"][mem["id"]]["color"] == source_color(
        mem["source"])


async def test_memory_nodes_carry_full_content(client):
    """Hover preview cards need the full text; the display label stays
    the 80-char prefix the table also uses."""
    from invincible.main import app

    made, _ = await register_account(client, "hoverer@example.com")
    uid = made.json()["id"]
    long = ("portfolio roadmap discussion " * 5).strip()  # > 80 chars
    store = MemoryStore(app.state.engine)
    await store.save_memory(user_id=uid, content=long)
    payload = await build_memory_projection(
        store, ProjectService(app.state.engine), user_id=uid)
    mem = next(n for n in payload["nodes"] if n["kind"] == "memory")
    assert mem["content"] == long       # full text for the preview card
    assert mem["label"] == long[:80]    # display string stays truncated


async def test_cross_user_isolation(client):
    from invincible.main import app

    made_a, _ = await register_account(client, "a@example.com")
    made_b, _ = await register_account(client, "b@example.com")
    uid_a, uid_b = made_a.json()["id"], made_b.json()["id"]
    await _seed(app, uid_a, count=3, provenance="mcp:claude")

    payload_b = await build_memory_projection(
        MemoryStore(app.state.engine), ProjectService(app.state.engine),
        user_id=uid_b)
    assert payload_b["summary"]["total"] == 0
    assert [n for n in payload_b["nodes"] if n["kind"] == "memory"] == []

    # Through the HTTP surface too: B's graph never contains A's rows.
    graph = (await client.get("/memories/graph")).json()
    assert graph["summary"]["total"] == 0


async def test_project_filter_uses_union_semantics(client):
    from invincible.main import app

    made, _ = await register_account(client, "union@example.com")
    uid = made.json()["id"]
    projects = ProjectService(app.state.engine)
    proj = await projects.create(uid, "website")

    await _seed(app, uid, count=2, project_id=proj["id"])  # project rows
    await _seed(app, uid, count=1)                          # user-scope row

    payload = await build_memory_projection(
        MemoryStore(app.state.engine), projects,
        user_id=uid, project_id=proj["id"])
    assert payload["summary"]["total"] == 3  # project + user-scope union
    scoped = [
        n for n in payload["nodes"] if n["kind"] == "memory"
        and n["project_id"] is not None
    ]
    assert len(scoped) == 2  # only the filtered project's rows appear


async def test_node_cap_truncates_visibly(client):
    from invincible.main import app

    made, _ = await register_account(client, "cap@example.com")
    uid = made.json()["id"]
    await _seed(app, uid, count=MEMORY_NODE_CAP + 50)

    payload = await build_memory_projection(
        MemoryStore(app.state.engine), ProjectService(app.state.engine),
        user_id=uid)
    assert payload["summary"]["total"] == MEMORY_NODE_CAP + 50
    assert payload["summary"]["shown"] == MEMORY_NODE_CAP
    assert payload["summary"]["truncated"] is True
    assert len([n for n in payload["nodes"]
                if n["kind"] == "memory"]) == MEMORY_NODE_CAP
    # The page announces the truncation instead of hiding it.
    page = await client.get("/dashboard/memory")
    assert "Showing the" in page.text


async def test_kind_filter_scopes_projection(client):
    from invincible.main import app

    made, _ = await register_account(client, "kinds@example.com")
    uid = made.json()["id"]
    await _seed(app, uid, count=2, kind="preference")
    await _seed(app, uid, count=1, kind="task")

    payload = await build_memory_projection(
        MemoryStore(app.state.engine), ProjectService(app.state.engine),
        user_id=uid, kind="preference")
    assert payload["summary"]["total"] == 2
    assert payload["summary"]["by_kind"] == {"preference": 2}


# --- JSON/page parity (the permanent contract) ------------------------------


async def test_json_and_page_share_the_projection(client):
    from invincible.main import app

    made, _ = await register_account(client, "parity@example.com")
    uid = made.json()["id"]
    await _seed(app, uid, count=2, provenance="mcp:grok")

    graph = (await client.get("/memories/graph")).json()
    page = await client.get("/dashboard/memory")
    assert graph["summary"]["total"] == 2
    assert page.status_code == 200
    # The page renders nodes the JSON reports: mcp:grok provenance dots.
    assert "mcp:grok" in page.text
    # Internal node ids never leak into the rendered page - tooltips
    # carry the memory text and source, not "memory:<id>".
    assert "memory:" not in page.text
    # Filters survive the round trip on both surfaces.
    filtered = (await client.get(
        "/memories/graph?kind=note")).json()
    assert filtered["summary"]["total"] == 2


# --- Level 2: similar_to derivation -------------------------------------------


def test_similar_edges_shared_tokens():
    tokens = {
        "memory:1": ["uses", "postgres", "pooling"],
        "memory:2": ["postgres", "pooling", "notes"],
        "memory:3": ["prefers", "dark", "mode"],
    }
    edges = similar_edges(tokens)
    assert edges == [
        {"source": "memory:1", "target": "memory:2",
         "kind": "similar_to", "weight": 2},
    ]


def test_similar_edges_short_contents_need_one_token():
    # Two terse memories sharing a single token still connect.
    tokens = {"memory:1": ["postgres"], "memory:2": ["postgres", "tips"]}
    assert similar_edges(tokens)[0]["weight"] == 1


def test_similar_edges_capped_per_node_and_total():
    # One hub id: every other id shares tokens with it, but its degree
    # can never exceed _SIMILAR_MAX_PER_NODE (4).
    tokens = {"memory:0": ["alpha", "beta", "gamma", "delta"]}
    for i in range(1, 20):
        tokens[f"memory:{i}"] = ["alpha", "beta", "gamma", f"v{i}"]
    edges = similar_edges(tokens)
    degree: dict[str, int] = {}
    for e in edges:
        degree[e["source"]] = degree.get(e["source"], 0) + 1
        degree[e["target"]] = degree.get(e["target"], 0) + 1
    assert all(d <= 4 for d in degree.values())
    assert len(edges) <= 200


async def test_projection_carries_keywords_and_similar_edges(client):
    from invincible.main import app

    made, _ = await register_account(client, "similar@example.com")
    uid = made.json()["id"]
    store = MemoryStore(app.state.engine)
    await store.save_memory(user_id=uid, kind="note",
                            content="uses postgres pooling everywhere")
    await store.save_memory(user_id=uid, kind="note",
                            content="postgres pooling needs monitoring")
    await store.save_memory(user_id=uid, kind="note",
                            content="prefers dark mode editors")

    payload = await build_memory_projection(
        MemoryStore(app.state.engine),
        ProjectService(app.state.engine), user_id=uid)
    mems = [n for n in payload["nodes"] if n["kind"] == "memory"]
    assert all(n["keywords"] is not None for n in mems)
    similar = [e for e in payload["edges"] if e["kind"] == "similar_to"]
    assert len(similar) == 1
    assert similar[0]["weight"] >= 2


async def test_graph_redirects_to_merged_page(client):
    await register_account(client, "redirect@example.com")
    resp = await client.get(
        "/dashboard/memory/graph", params={"kind": "note"},
        follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/memory?kind=note"
    # Anonymous stays gated: 401, never a redirect loop.
    client.cookies.clear()
    anon = await client.get(
        "/dashboard/memory/graph", follow_redirects=False)
    assert anon.status_code == 401
