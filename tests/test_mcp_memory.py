# tests/test_mcp_memory.py
"""MCP memory tools: memory_save / memory_search / memory_list, plus the
project_create / project_list tools that feed them valid project names.

Data-plane tools over the shared memories store: saves land under the
OAuth subject with mcp:<client> provenance and 0.9 confidence, search
runs the same RetrievalService ranking gateway chat uses, and every
query is ownership-predicated (a foreign user's rows are
indistinguishable from absent ones). No confirm_action gate applies -
rows are user-owned data, reversible from the dashboard.
"""
import asyncio
import json
import time

from sqlalchemy import text

from invincible.core.db import ensure_local_owner
from invincible.core.memory import MCP_CONFIDENCE
from invincible.main import app


async def _call_tool(client, headers, name, arguments):
    response = await client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200
    return response.json()["result"]


def _tool_json(result):
    assert result["isError"] is False, result["content"][0]["text"]
    return json.loads(result["content"][0]["text"])


async def _bearer_user_id() -> int:
    """The OAuth subject behind the bearer_headers fixture: the owner-
    secret browser login resolves to the system local owner, so memory
    rows land under that user."""
    return (await ensure_local_owner(app.state.engine))[0]


async def _create_project(user_id: int, name: str) -> int:
    async with app.state.engine.begin() as conn:
        return int((await conn.execute(
            text(
                "INSERT INTO projects"
                " (user_id, name, is_default, created_at)"
                " VALUES (:u, :n, false, :t) RETURNING id"
            ),
            {"u": user_id, "n": name, "t": time.time()},
        )).scalar_one())


# --- memory_save -----------------------------------------------------------


async def test_memory_save_lands_under_oauth_subject(client, bearer_headers):
    result = _tool_json(await _call_tool(client, bearer_headers,
                                         "memory_save", {
        "content": "Tareq prefers Python for scripting",
        "kind": "preference",
    }))
    assert result["saved"] is True
    assert result["scope"] == "user"
    assert result["kind"] == "preference"

    uid = await _bearer_user_id()
    rows = await app.state.memory.list_for_user(uid)
    row = next(r for r in rows if r["id"] == result["id"])
    assert row["content"] == "Tareq prefers Python for scripting"
    assert row["kind"] == "preference"
    assert row["layer"] == "explicit"
    assert row["confidence"] == MCP_CONFIDENCE
    # obtain_access_token registers its client as "test-client".
    assert row["provenance"] == "mcp:test-client"


async def test_memory_save_defaults_kind_to_note(client, bearer_headers):
    result = _tool_json(await _call_tool(client, bearer_headers,
                                         "memory_save", {
        "content": "deploys via railway then azure",
    }))
    assert result["kind"] == "note"


async def test_memory_save_requires_content(client, bearer_headers):
    result = await _call_tool(client, bearer_headers, "memory_save", {})
    assert result["isError"] is True
    assert "content" in result["content"][0]["text"]


async def test_memory_save_rejects_oversized_content(
        client, bearer_headers):
    result = await _call_tool(client, bearer_headers, "memory_save", {
        "content": "x" * 2001,
    })
    assert result["isError"] is True
    assert "2000" in result["content"][0]["text"]


async def test_memory_save_rejects_unknown_kind(client, bearer_headers):
    result = await _call_tool(client, bearer_headers, "memory_save", {
        "content": "something true", "kind": "epiphany",
    })
    assert result["isError"] is True
    assert "kind" in result["content"][0]["text"]


async def test_memory_save_to_project_by_name(client, bearer_headers):
    uid = await _bearer_user_id()
    project_id = await _create_project(uid, "invincible")

    result = _tool_json(await _call_tool(client, bearer_headers,
                                         "memory_save", {
        "content": "invincible gateway runs on railway",
        "project": "invincible",
    }))
    assert result["scope"] == "project"

    rows = await app.state.memory.list_for_user(
        uid, project_id=project_id)
    assert any(r["id"] == result["id"] for r in rows)


async def test_memory_save_unknown_project_errors(client, bearer_headers):
    result = await _call_tool(client, bearer_headers, "memory_save", {
        "content": "fact about nowhere", "project": "does-not-exist",
    })
    assert result["isError"] is True
    assert "Unknown project" in result["content"][0]["text"]


# --- kill-switch -----------------------------------------------------------


async def test_kill_switch_blocks_save_not_search(
        client, bearer_headers, monkeypatch):
    _tool_json(await _call_tool(client, bearer_headers, "memory_save", {
        "content": "azure migration planned for october",
    }))

    monkeypatch.setenv("INVINCIBLE_MEMORY", "0")
    blocked = await _call_tool(client, bearer_headers, "memory_save", {
        "content": "this must never land",
    })
    assert blocked["isError"] is True
    assert "disabled" in blocked["content"][0]["text"]

    # Search/list keep working so the toggle never traps saved data.
    found = _tool_json(await _call_tool(client, bearer_headers,
                                        "memory_search", {
        "query": "azure migration",
    }))
    assert found["count"] == 1
    listed = _tool_json(await _call_tool(client, bearer_headers,
                                         "memory_list", {}))
    assert all("never land" not in m["content"]
               for m in listed["memories"])


# --- memory_search ---------------------------------------------------------


async def test_memory_search_returns_relevant_not_irrelevant(
        client, bearer_headers):
    _tool_json(await _call_tool(client, bearer_headers, "memory_save", {
        "content": "deployment: railway to azure migration",
    }))
    _tool_json(await _call_tool(client, bearer_headers, "memory_save", {
        "content": "preference: dark themes in editors",
    }))

    result = _tool_json(await _call_tool(client, bearer_headers,
                                         "memory_search", {
        "query": "azure migration",
    }))
    contents = [r["content"] for r in result["results"]]
    assert "deployment: railway to azure migration" in contents
    assert all("dark themes" not in c for c in contents)
    top = result["results"][0]
    assert top["relevance"] > 0  # ranked rows carry a relevance hint


async def test_memory_search_weak_query_hits_relevance_floor(
        client, bearer_headers):
    _tool_json(await _call_tool(client, bearer_headers, "memory_save", {
        "content": "deployment: railway to azure migration",
    }))
    result = _tool_json(await _call_tool(client, bearer_headers,
                                         "memory_search", {
        "query": "qqqwwwzzz nothing matches this",
    }))
    assert result["count"] == 0
    assert result["results"] == []


async def test_memory_search_requires_query(client, bearer_headers):
    result = await _call_tool(client, bearer_headers, "memory_search", {})
    assert result["isError"] is True
    assert "query" in result["content"][0]["text"]


async def test_memory_search_never_sees_other_users_rows(
        client, bearer_headers):
    # A second user with a distinctive memory, inserted directly.
    async with app.state.engine.begin() as conn:
        other_id = int((await conn.execute(
            text(
                "INSERT INTO users (email, created_at)"
                " VALUES ('other@example.com', :t) RETURNING id"
            ),
            {"t": time.time()},
        )).scalar_one())
        await conn.execute(
            text(
                "INSERT INTO memories"
                " (user_id, project_id, scope, layer, kind, content,"
                "  confidence, provenance, created_at)"
                " VALUES (:u, NULL, 'user', 'explicit', 'fact',"
                "         'secret kiwi plantation', 1.0, 'chat:other', :t)"
            ),
            {"u": other_id, "t": time.time()},
        )

    searched = _tool_json(await _call_tool(client, bearer_headers,
                                           "memory_search", {
        "query": "kiwi plantation",
    }))
    assert searched["count"] == 0

    listed = _tool_json(await _call_tool(client, bearer_headers,
                                         "memory_list", {}))
    assert all("kiwi" not in m["content"] for m in listed["memories"])


async def test_memory_search_project_scope_union(client, bearer_headers):
    uid = await _bearer_user_id()
    invincible_id = await _create_project(uid, "invincible")
    await _create_project(uid, "unrelated")

    # User-scope row (via the MCP tool itself).
    _tool_json(await _call_tool(client, bearer_headers, "memory_save", {
        "content": "prefers concise answers",
    }))
    # Project rows via the store directly.
    await app.state.memory.save_memory(
        user_id=uid, content="invincible deploys via railway",
        layer="explicit", kind="fact", confidence=1.0,
        provenance="test", project_id=invincible_id)
    await app.state.memory.save_memory(
        user_id=uid, content="unrelated project grows kiwis",
        layer="explicit", kind="fact", confidence=1.0,
        provenance="test",
        project_id=(await _create_project(uid, "kiwi-farm")))

    scoped = _tool_json(await _call_tool(client, bearer_headers,
                                         "memory_search", {
        "query": "railway", "project": "invincible",
    }))
    assert scoped["count"] == 1
    assert "railway" in scoped["results"][0]["content"]

    # The union includes user-scope ("who you are") memories too.
    who = _tool_json(await _call_tool(client, bearer_headers,
                                      "memory_search", {
        "query": "concise answers", "project": "invincible",
    }))
    assert who["count"] == 1
    assert "concise" in who["results"][0]["content"]

    # Other projects' rows never leak into a scoped search.
    leaked = _tool_json(await _call_tool(client, bearer_headers,
                                         "memory_search", {
        "query": "kiwis", "project": "invincible",
    }))
    assert leaked["count"] == 0


# --- memory_list -----------------------------------------------------------


async def test_memory_list_newest_first_with_kind_filter(
        client, bearer_headers):
    _tool_json(await _call_tool(client, bearer_headers, "memory_save", {
        "content": "first saved decision", "kind": "decision",
    }))
    _tool_json(await _call_tool(client, bearer_headers, "memory_save", {
        "content": "second saved note",
    }))

    listed = _tool_json(await _call_tool(client, bearer_headers,
                                         "memory_list", {}))
    contents = [m["content"] for m in listed["memories"]]
    # Newest first (id breaks created_at ties).
    assert contents.index("second saved note") \
        < contents.index("first saved decision")

    decisions = _tool_json(await _call_tool(client, bearer_headers,
                                            "memory_list", {
        "kind": "decision",
    }))
    assert [m["content"] for m in decisions["memories"]] \
        == ["first saved decision"]


async def test_memory_list_limit_capped(client, bearer_headers):
    uid = await _bearer_user_id()
    for i in range(21):
        await app.state.memory.save_memory(
            user_id=uid, content=f"bulk memory row number {i}",
            layer="explicit", kind="note", confidence=1.0,
            provenance="test")

    listed = _tool_json(await _call_tool(client, bearer_headers,
                                         "memory_list", {
        "limit": 999,  # hard cap wins over the caller's ask
    }))
    assert listed["count"] == 20


# --- project_create / project_list -----------------------------------------


async def test_project_create_then_memory_save_to_project(
        client, bearer_headers):
    """The end-to-end gap this tool closes: create a project over MCP,
    then immediately use its name as memory_save's project argument."""
    made = _tool_json(await _call_tool(client, bearer_headers,
                                       "project_create", {
        "name": "invincible",
    }))
    assert made["created"] is True
    assert made["name"] == "invincible"

    saved = _tool_json(await _call_tool(client, bearer_headers,
                                        "memory_save", {
        "content": "invincible gateway runs on railway",
        "project": "invincible",
    }))
    assert saved["saved"] is True
    assert saved["scope"] == "project"


async def test_project_create_requires_name(client, bearer_headers):
    result = await _call_tool(client, bearer_headers, "project_create", {})
    assert result["isError"] is True
    assert "name" in result["content"][0]["text"]


async def test_project_create_rejects_blank_after_trim(
        client, bearer_headers):
    result = await _call_tool(client, bearer_headers, "project_create", {
        "name": "   ",
    })
    assert result["isError"] is True
    assert "name" in result["content"][0]["text"]


async def test_project_create_rejects_oversized_name(client, bearer_headers):
    result = await _call_tool(client, bearer_headers, "project_create", {
        "name": "x" * 101,
    })
    assert result["isError"] is True
    assert "100" in result["content"][0]["text"]


async def test_project_create_duplicate_rejected(client, bearer_headers):
    first = _tool_json(await _call_tool(client, bearer_headers,
                                        "project_create", {
        "name": "solo",
    }))
    assert first["created"] is True

    dup = await _call_tool(client, bearer_headers, "project_create", {
        "name": "solo",
    })
    assert dup["isError"] is True
    assert "already have a project" in dup["content"][0]["text"]


async def test_project_create_concurrent_same_name_one_wins(
        client, bearer_headers):
    """Two creates racing on the same name: the (user_id, name) unique
    constraint is the arbiter, so exactly one succeeds."""
    results = await asyncio.gather(
        _call_tool(client, bearer_headers, "project_create",
                   {"name": "raced"}),
        _call_tool(client, bearer_headers, "project_create",
                   {"name": "raced"}),
    )
    outcomes = [r["isError"] for r in results]
    assert outcomes.count(False) == 1
    assert outcomes.count(True) == 1


async def test_project_create_cap_enforced(client, bearer_headers,
                                           monkeypatch):
    from invincible.endpoints import mcp as mcp_module
    monkeypatch.setattr(mcp_module, "_PROJECT_CAP", 3)

    # Earlier tests in this run may have left projects on the shared
    # local-owner account, so the baseline is whatever exists now.
    listing = _tool_json(await _call_tool(client, bearer_headers,
                                          "project_list", {}))
    existing = listing["count"]
    for i in range(existing, 3):  # fill up to the cap (no-op when at it)
        _tool_json(await _call_tool(client, bearer_headers,
                                    "project_create", {
            "name": f"filler-{i}",
        }))

    over = await _call_tool(client, bearer_headers, "project_create", {
        "name": "one-too-many",
    })
    assert over["isError"] is True
    assert "limit" in over["content"][0]["text"]

    # The over-cap create was rolled back: the count never grows past the
    # cap and the row is gone.
    after = _tool_json(await _call_tool(client, bearer_headers,
                                        "project_list", {}))
    assert after["count"] == max(existing, 3)
    assert all(p["name"] != "one-too-many" for p in after["projects"])


async def test_project_list_shows_only_callers_projects(
        client, bearer_headers):
    _tool_json(await _call_tool(client, bearer_headers, "project_create", {
        "name": "mine-only",
    }))

    # A second user's project, inserted directly - must never appear.
    async with app.state.engine.begin() as conn:
        other_id = int((await conn.execute(
            text(
                "INSERT INTO users (email, created_at)"
                " VALUES ('other-projects@example.com', :t) RETURNING id"
            ),
            {"t": time.time()},
        )).scalar_one())
        await conn.execute(
            text(
                "INSERT INTO projects (user_id, name, is_default,"
                " created_at) VALUES (:u, 'secret kiwi orchard', false,"
                " :t)"
            ),
            {"u": other_id, "t": time.time()},
        )

    listed = _tool_json(await _call_tool(client, bearer_headers,
                                         "project_list", {}))
    names = [p["name"] for p in listed["projects"]]
    assert "mine-only" in names
    # The auto-created default project (named "local" for the test
    # suite's local-owner subject, "personal" for normal accounts).
    assert any(p["is_default"] for p in listed["projects"])
    assert "secret kiwi orchard" not in names


async def test_project_list_hides_archived_by_default(
        client, bearer_headers):
    made = _tool_json(await _call_tool(client, bearer_headers,
                                       "project_create", {
        "name": "to-archive",
    }))
    async with app.state.engine.begin() as conn:
        await conn.execute(
            text("UPDATE projects SET archived_at = :t"
                 " WHERE id = :id"),
            {"t": time.time(), "id": made["id"]},
        )

    hidden = _tool_json(await _call_tool(client, bearer_headers,
                                         "project_list", {}))
    assert all(p["name"] != "to-archive" for p in hidden["projects"])

    shown = _tool_json(await _call_tool(client, bearer_headers,
                                        "project_list", {
        "include_archived": True,
    }))
    assert any(p["name"] == "to-archive" and p["archived_at"]
               for p in shown["projects"])


async def test_project_tools_in_tools_list(client, bearer_headers):
    response = await client.post(
        "/mcp",
        headers=bearer_headers,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    names = [t["name"] for t in response.json()["result"]["tools"]]
    assert "project_create" in names
    assert "project_list" in names
