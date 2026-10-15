# tests/test_agent_prompt.py
"""Bluey-style agent prompt export pins (pure projection, no DB)."""

from invincible.core.projection import format_agent_prompt


def test_formats_roles_oldest_first():
    out = format_agent_prompt("abc", [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ])
    assert "Session abc transcript" in out
    assert "User: hello" in out
    assert "Assistant: hi there" in out
    assert out.index("User:") < out.index("Assistant:")


def test_skips_empty_and_caps_length():
    out = format_agent_prompt("s", [
        {"role": "user", "content": "  "},
        {"role": "user", "content": "x" * 2000},
    ])
    assert "User: " + "x" * 1000 in out
    assert len(out) <= 6200


def test_never_raises_on_malformed():
    out = format_agent_prompt("s", [None, "x", {"role": "user"}])
    assert "Session s transcript" in out


def test_asked_renders_as_you_asked():
    out = format_agent_prompt("s", [
        {"role": "user", "content": "background noise", "asked": False},
        {"role": "user", "content": "real question", "asked": True},
    ])
    assert "User: background noise" in out
    assert "You asked: real question" in out


def test_asked_stripped_for_routing_but_kept_for_persist():
    from invincible.core.chat_service import _sanitize_for_routing
    from invincible.endpoints.chat import _user_message
    msg = _user_message("hi", {"asked": True})
    assert msg == {"role": "user", "content": "hi", "asked": True}
    assert _user_message("hi", {}) == {"role": "user", "content": "hi"}
    routed = _sanitize_for_routing([msg])
    assert routed == [{"role": "user", "content": "hi"}]
    assert msg.get("asked") is True
