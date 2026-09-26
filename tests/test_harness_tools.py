# tests/test_harness_tools.py
"""H6a: code_search / process_list / screenshot — hermetic unit tests plus
live MCP dispatch tests (bearer_headers walks the real OAuth flow).

Both execution sides run the EXACT shared helpers, so these pin the
result shapes once; the runner tests pin the sandbox gating.
"""
import json
import os
import shutil

import pytest

from invincible.agent.runner import execute_job
from invincible.core import tool_executor


def _tree(tmp_path):
    (tmp_path / "a.py").write_text("def hello():\n    return 'world'\n")
    (tmp_path / "b.txt").write_text("nothing to see here\n")
    (tmp_path / "big.bin").write_bytes(b"\x00" * 100)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.py").write_text("# hello again\nx = 1\n")
    skipped = tmp_path / "node_modules"
    skipped.mkdir()
    (skipped / "d.py").write_text("hello hidden\n")
    return tmp_path


async def test_search_finds_matches_and_skips(tmp_path):
    root = _tree(tmp_path)
    out = await tool_executor._search_code("hello", str(root), 20)
    assert out["status"] == "search"
    paths = {h["path"] for h in out["hits"]}
    assert any(p.endswith("a.py") for p in paths)
    assert any(p.endswith("c.py") for p in paths)
    assert not any("node_modules" in p for p in paths)
    assert not any(p.endswith("big.bin") for p in paths)
    assert all(h["line"] >= 1 for h in out["hits"])
    assert out["files_searched"] >= 3


async def test_search_respects_max_results(tmp_path):
    root = _tree(tmp_path)
    (root / "many.txt").write_text("hello\n" * 100)
    out = await tool_executor._search_code("hello", str(root), 5)
    assert len(out["hits"]) == 5
    assert out["truncated"] is True


async def test_search_errors(tmp_path):
    out = await tool_executor._search_code(
        "", str(tmp_path), 20)
    assert out["status"] == "error"
    out = await tool_executor._search_code(
        "x", str(tmp_path / "missing"), 20)
    assert out["status"] == "error"


async def test_search_single_file(tmp_path):
    target = tmp_path / "only.py"
    target.write_text("needle in a haystack\n")
    out = await tool_executor._search_code(
        "needle", str(target), 20)
    assert out["status"] == "search"
    assert len(out["hits"]) == 1
    assert out["hits"][0]["line"] == 1


async def test_walk_fallback_matches_rg_shape(tmp_path, monkeypatch):
    """The Python walker honors skip dirs + caps even when rg exists."""
    root = _tree(tmp_path)

    async def _none(*args):
        return None

    monkeypatch.setattr(tool_executor, "_search_with_rg", _none)
    out = await tool_executor._search_code("hello", str(root), 20)
    assert out["status"] == "search"
    assert any(h["path"].endswith("a.py") for h in out["hits"])


async def test_rg_fast_path_when_available(tmp_path):
    if shutil.which("rg") is None:
        pytest.skip("ripgrep not installed")
    root = _tree(tmp_path)
    out = await tool_executor._search_with_rg("hello", str(root), 20)
    assert out is not None
    hits, _, _ = out
    assert any("a.py" in h["path"] for h in hits)


async def test_search_server_gate_blocks_outside_roots(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(tool_executor.ToolBlocked):
        await tool_executor.search_code(
            "x", os.path.abspath(os.path.join("..", "x")), 5)


async def test_list_processes_shape():
    out = await tool_executor._list_processes(5)
    assert out["status"] == "processes"
    assert len(out["processes"]) <= 5
    assert all(isinstance(p["pid"], int) for p in out["processes"])
    assert all(p["name"] for p in out["processes"])


async def test_screenshot_refuses_non_http():
    out = await tool_executor._take_screenshot("file:///etc/passwd", 5.0)
    assert out["status"] == "error"
    assert "http" in out["error"]


async def test_screenshot_unavailable_without_chrome(monkeypatch):
    monkeypatch.setattr(tool_executor, "_find_chrome", lambda: None)
    out = await tool_executor._take_screenshot(
        "http://127.0.0.1:8000/", 5.0)
    assert out["status"] == "unavailable"
    assert "INVINCIBLE_CHROME_BIN" in out["reason"]


def _no_chrome(monkeypatch):
    """Force every discovery tier to miss (hermetic, no real browser)."""
    monkeypatch.delenv("INVINCIBLE_CHROME_BIN", raising=False)
    monkeypatch.setattr(tool_executor.shutil, "which", lambda name: None)
    monkeypatch.setattr(tool_executor, "_chrome_common_paths", lambda: [])
    monkeypatch.setattr(
        tool_executor, "_chrome_registry_candidates", lambda: [])


def test_find_chrome_override_wins(monkeypatch, tmp_path):
    target = tmp_path / "chrome.exe"
    target.write_text("x")
    monkeypatch.setenv("INVINCIBLE_CHROME_BIN", str(target))
    assert tool_executor._find_chrome() == str(target)


def test_find_chrome_missing_override_falls_through(monkeypatch):
    monkeypatch.setenv(
        "INVINCIBLE_CHROME_BIN", r"C:\nope\chrome.exe")
    monkeypatch.setattr(
        tool_executor.shutil, "which",
        lambda name: r"C:\PATH\chrome.exe" if name == "chrome" else None)
    monkeypatch.setattr(tool_executor, "_chrome_common_paths", lambda: [])
    monkeypatch.setattr(
        tool_executor, "_chrome_registry_candidates", lambda: [])
    assert tool_executor._find_chrome() == r"C:\PATH\chrome.exe"


def test_find_chrome_path_before_common_paths(monkeypatch):
    _no_chrome(monkeypatch)
    monkeypatch.setattr(
        tool_executor.shutil, "which",
        lambda name: "/usr/bin/chromium" if name == "chromium" else None)
    monkeypatch.setattr(
        tool_executor, "_chrome_common_paths",
        lambda: ["/Applications/Never/Chrome"])
    assert tool_executor._find_chrome() == "/usr/bin/chromium"


def test_find_chrome_common_path_when_path_misses(
        monkeypatch, tmp_path):
    target = tmp_path / "chrome"
    target.write_text("x")
    monkeypatch.delenv("INVINCIBLE_CHROME_BIN", raising=False)
    monkeypatch.setattr(tool_executor.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        tool_executor, "_chrome_common_paths", lambda: [str(target)])
    monkeypatch.setattr(
        tool_executor, "_chrome_registry_candidates", lambda: [])
    assert tool_executor._find_chrome() == str(target)


def test_find_chrome_registry_last_resort(monkeypatch, tmp_path):
    target = tmp_path / "msedge.exe"
    target.write_text("x")
    monkeypatch.delenv("INVINCIBLE_CHROME_BIN", raising=False)
    monkeypatch.setattr(tool_executor.shutil, "which", lambda name: None)
    monkeypatch.setattr(tool_executor, "_chrome_common_paths", lambda: [])
    monkeypatch.setattr(
        tool_executor, "_chrome_registry_candidates",
        lambda: [str(target)])
    assert tool_executor._find_chrome() == str(target)


def test_find_chrome_none_when_everything_misses(monkeypatch):
    _no_chrome(monkeypatch)
    assert tool_executor._find_chrome() is None


def test_hello_frame_advertises_chrome_via_finder(monkeypatch):
    from invincible.agent import runner

    monkeypatch.setattr(
        tool_executor, "_find_chrome", lambda: "/usr/bin/chrome")
    assert runner.hello_frame()["capabilities"]["chrome"] is True
    monkeypatch.setattr(tool_executor, "_find_chrome", lambda: None)
    assert runner.hello_frame()["capabilities"]["chrome"] is False


async def test_runner_search_blocked_outside_sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("INVINCIBLE_AGENT_ROOT", str(tmp_path))
    out = await execute_job({
        "job_id": "j1", "type": "code_search",
        "args": {"pattern": "x", "path": "/etc", "max_results": 5},
    })
    assert out["status"] == "blocked"


async def test_runner_search_inside_sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("INVINCIBLE_AGENT_ROOT", str(tmp_path))
    (tmp_path / "note.txt").write_text("find me please\n")
    out = await execute_job({
        "job_id": "j2", "type": "code_search",
        "args": {"pattern": "find me", "path": str(tmp_path),
                 "max_results": 5},
    })
    assert out["status"] == "search"
    assert len(out["hits"]) == 1


async def test_runner_processes_and_screenshot_shapes(monkeypatch):
    out = await execute_job({
        "job_id": "j3", "type": "process_list", "args": {"limit": 3}})
    assert out["status"] == "processes"
    out = await execute_job({
        "job_id": "j4", "type": "screenshot",
        "args": {"url": "not-a-url"}})
    assert out["status"] == "error"


def _dir_tree(tmp_path):
    (tmp_path / "b.py").write_text("x = 1\n")
    (tmp_path / "a.txt").write_text("hello\n")
    (tmp_path / ".hidden").write_text("shh\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.py").write_text("y = 2\n")
    return tmp_path


async def test_list_dir_shape_and_order(tmp_path):
    root = _dir_tree(tmp_path)
    out = await tool_executor._list_dir(str(root), 100)
    assert out["status"] == "directory"
    assert out["truncated"] is False
    names = [e["name"] for e in out["entries"]]
    # dirs first, then alpha; hidden skipped by default.
    assert names == ["sub", "a.txt", "b.py"]
    by_name = {e["name"]: e for e in out["entries"]}
    assert by_name["sub"]["type"] == "dir"
    assert by_name["a.txt"]["type"] == "file"
    assert by_name["a.txt"]["size"] > 0


async def test_list_dir_hidden_and_cap(tmp_path):
    root = _dir_tree(tmp_path)
    out = await tool_executor._list_dir(str(root), 100, True)
    assert ".hidden" in {e["name"] for e in out["entries"]}
    out = await tool_executor._list_dir(str(root), 2)
    assert len(out["entries"]) == 2
    assert out["truncated"] is True


async def test_list_dir_errors(tmp_path):
    out = await tool_executor._list_dir(
        str(tmp_path / "missing"), 10)
    assert out["status"] == "error"
    target = tmp_path / "f.txt"
    target.write_text("x\n")
    out = await tool_executor._list_dir(str(target), 10)
    assert out["status"] == "error"


async def test_list_dir_server_gate_blocks_outside_roots(
        tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(tool_executor.ToolBlocked):
        await tool_executor.list_dir(
            os.path.abspath(os.path.join("..", "x")), 10)


async def test_runner_list_dir_sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("INVINCIBLE_AGENT_ROOT", str(tmp_path))
    (tmp_path / "note.txt").write_text("hi\n")
    out = await execute_job({
        "job_id": "j5", "type": "list_dir",
        "args": {"path": str(tmp_path), "limit": 10},
    })
    assert out["status"] == "directory"
    assert any(e["name"] == "note.txt" for e in out["entries"])
    out = await execute_job({
        "job_id": "j6", "type": "list_dir",
        "args": {"path": "/etc", "limit": 10},
    })
    assert out["status"] == "blocked"


def _git_repo(tmp_path):
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"],
                   cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"],
                   cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "a.txt").write_text("one\n")
    subprocess.run(["git", "add", "a.txt"], cwd=tmp_path, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "first"],
                   cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "a.txt").write_text("two\n")
    return tmp_path


async def test_git_status_clean_and_dirty(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    root = _git_repo(tmp_path)
    out = await tool_executor._git_status(str(root))
    assert out["status"] == "git_status"
    assert out["clean"] is False
    assert any("a.txt" in line for line in out["changes"])
    assert out["branch"] != ""


async def test_git_diff_and_log(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    root = _git_repo(tmp_path)
    out = await tool_executor._git_diff(str(root))
    assert out["status"] == "git_diff"
    assert "two" in out["diff"]
    assert out["truncated"] is False
    out = await tool_executor._git_log(str(root), 5)
    assert out["status"] == "git_log"
    assert len(out["commits"]) == 1
    assert out["commits"][0]["subject"] == "first"
    assert len(out["commits"][0]["hash"]) == 40


async def test_git_not_a_repo_is_error(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    (tmp_path / "plain.txt").write_text("x\n")
    for coro in (tool_executor._git_status(str(tmp_path)),
                 tool_executor._git_diff(str(tmp_path)),
                 tool_executor._git_log(str(tmp_path), 5)):
        out = await coro
        assert out["status"] == "error"


async def test_git_wrappers_gate_outside_roots(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    outside = os.path.abspath(os.path.join("..", "x"))
    with pytest.raises(tool_executor.ToolBlocked):
        await tool_executor.git_status(outside)
    with pytest.raises(tool_executor.ToolBlocked):
        await tool_executor.git_diff(outside)
    with pytest.raises(tool_executor.ToolBlocked):
        await tool_executor.git_log(outside, 5)


async def test_runner_git_sandbox(tmp_path, monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    monkeypatch.setenv("INVINCIBLE_AGENT_ROOT", str(tmp_path))
    root = _git_repo(tmp_path)
    out = await execute_job({
        "job_id": "j7", "type": "git_status",
        "args": {"path": str(root)},
    })
    assert out["status"] == "git_status"
    out = await execute_job({
        "job_id": "j8", "type": "git_log",
        "args": {"path": str(root), "limit": 5},
    })
    assert out["status"] == "git_log"
    out = await execute_job({
        "job_id": "j9", "type": "git_diff",
        "args": {"path": "/etc"},
    })
    assert out["status"] == "blocked"


# --- live MCP dispatch (needs Postgres via client fixture) ---


async def _call(client, headers, name, arguments):
    response = await client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    assert response.status_code == 200
    body = response.json()
    assert "result" in body
    return body["result"]


async def test_mcp_code_search_round_trip(client, bearer_headers, tmp_path,
                                          monkeypatch):
    from invincible.core import settings as settings_module

    monkeypatch.setattr(
        settings_module.settings, "read_roots",
        lambda: [str(tmp_path)])
    (tmp_path / "app.py").write_text("def target_fn():\n    pass\n")
    result = await _call(client, bearer_headers, "code_search", {
        "pattern": "target_fn", "path": str(tmp_path), "max_results": 5})
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "search"
    assert any(h["path"].endswith("app.py") for h in payload["hits"])


async def test_mcp_code_search_blocked_outside_roots(client, bearer_headers):
    result = await _call(client, bearer_headers, "code_search", {
        "pattern": "x", "path": "/etc", "max_results": 5})
    assert result["isError"] is True
    assert "Blocked" in result["content"][0]["text"]


async def test_mcp_process_list_round_trip(client, bearer_headers):
    result = await _call(client, bearer_headers, "process_list",
                         {"limit": 5})
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "processes"
    assert len(payload["processes"]) <= 5


async def test_mcp_list_dir_round_trip(client, bearer_headers, tmp_path,
                                       monkeypatch):
    from invincible.core import settings as settings_module

    monkeypatch.setattr(
        settings_module.settings, "read_roots",
        lambda: [str(tmp_path)])
    (tmp_path / "app.py").write_text("x = 1\n")
    result = await _call(client, bearer_headers, "list_dir", {
        "path": str(tmp_path), "limit": 10})
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "directory"
    assert any(e["name"] == "app.py" for e in payload["entries"])


async def test_mcp_list_dir_blocked_outside_roots(client, bearer_headers):
    result = await _call(client, bearer_headers, "list_dir",
                         {"path": "/etc", "limit": 10})
    assert result["isError"] is True
    assert "Blocked" in result["content"][0]["text"]


async def test_mcp_git_round_trip(client, bearer_headers, tmp_path,
                                  monkeypatch):
    import subprocess

    from invincible.core import settings as settings_module

    if shutil.which("git") is None:
        pytest.skip("git not installed")
    monkeypatch.setattr(
        settings_module.settings, "read_roots",
        lambda: [str(tmp_path)])
    subprocess.run(["git", "init"], cwd=tmp_path, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"],
                   cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"],
                   cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "a.txt").write_text("one\n")
    subprocess.run(["git", "add", "a.txt"], cwd=tmp_path, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "first"],
                   cwd=tmp_path, check=True, capture_output=True)
    result = await _call(client, bearer_headers, "git_log",
                         {"path": str(tmp_path), "limit": 5})
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "git_log"
    assert payload["commits"][0]["subject"] == "first"
    result = await _call(client, bearer_headers, "git_status",
                         {"path": str(tmp_path)})
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "git_status"


async def test_mcp_git_not_a_repo(client, bearer_headers, tmp_path,
                                  monkeypatch):
    from invincible.core import settings as settings_module

    if shutil.which("git") is None:
        pytest.skip("git not installed")
    monkeypatch.setattr(
        settings_module.settings, "read_roots",
        lambda: [str(tmp_path)])
    result = await _call(client, bearer_headers, "git_status",
                         {"path": str(tmp_path)})
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "error"


async def test_mcp_screenshot_unavailable_without_routing(
        client, bearer_headers):
    """Default mode (no agent routing): agent-only tool reports
    unavailability — the server never fetches caller URLs."""
    result = await _call(client, bearer_headers, "screenshot",
                         {"url": "http://127.0.0.1:9/"})
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "unavailable"
    assert "invincible harness connect" in payload["reason"]
