"""Unit tests for the context relay (core/relay.py).

Hermetic: pure functions on plain dicts, no fixtures, no Postgres. The
token threshold is pushed down via env so tiny fixtures trigger the relay
deterministically.
"""
import json

import pytest

from invincible.core.relay import RelayStats, relay_enabled, relay_messages


def _user(text):
    return {"role": "user", "content": text}


def _assistant(text):
    return {"role": "assistant", "content": text}


def _assistant_tool_call(call_id, name, arguments='{"path": "a.py"}'):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        ],
    }


def _tool_result(call_id, content):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _turn(i, tool=False):
    """One user/assistant turn, tool-flavored when requested."""
    messages = [_user(f"question {i} " + "x" * 50)]
    if tool:
        messages.append(_assistant_tool_call(f"call-{i}", "read_file"))
        messages.append(_tool_result(f"call-{i}", "y" * 200))
    else:
        messages.append(_assistant(f"answer {i} " + "z" * 50))
    return messages


def _history(turns, tool_every=None):
    messages = []
    for i in range(turns):
        messages.extend(_turn(i, tool=tool_every is not None and i % tool_every == 0))
    return messages


@pytest.fixture
def relay_on(monkeypatch):
    """Trigger the relay on tiny histories: threshold 10 tokens."""
    monkeypatch.setenv("INVINCIBLE_RELAY_THRESHOLD_TOKENS", "10")
    return monkeypatch


def test_below_threshold_passthrough():
    messages = _history(3)
    sent, stats = relay_messages(messages)
    assert sent is messages
    assert stats == RelayStats()


def test_old_turns_replaced_newest_kept_verbatim(relay_on):
    relay_on.setenv("INVINCIBLE_RELAY_KEEP_TURNS", "2")
    messages = _history(5)
    snapshot = [json.dumps(m, sort_keys=True) for m in messages]
    sent, stats = relay_messages(messages)

    assert stats.applied is True
    assert stats.turns_digested == 3
    # Exactly one digest message inserted.
    digests = [m for m in sent if m.get("role") == "system"]
    assert len(digests) == 1
    # The kept tail is byte-identical (same objects, same order).
    assert sent[-4:] == messages[-4:]
    assert [json.dumps(m, sort_keys=True) for m in sent[-4:]] == snapshot[-4:]


def test_injected_system_messages_survive_and_precede_the_digest(relay_on):
    relay_on.setenv("INVINCIBLE_RELAY_KEEP_TURNS", "1")
    system_prompt = {"role": "system", "content": "You are the client prompt."}
    memory_injection = {"role": "system", "content": "Memory: user likes rust."}
    messages = (
        [system_prompt, memory_injection]
        + _history(4)
        + [{"role": "system", "content": "mid-stream system"}]
    )
    sent, stats = relay_messages(messages)

    assert stats.applied is True
    assert sent[0] is system_prompt
    assert sent[1] is memory_injection
    digests = [
        m for m in sent
        if m.get("role") == "system"
        and "[Context relay]" in m.get("content", "")
    ]
    assert len(digests) == 1
    # The mid-stream system message is hoisted up with the others
    # (trim_messages does the same), so the digest lands at index 3.
    assert sent.index(digests[0]) == 3
    # Every original system message passes through verbatim, wherever it sat.
    for original in (system_prompt, memory_injection):
        assert original in sent
    assert {"role": "system", "content": "mid-stream system"} in sent


def test_digest_content_and_shape(relay_on):
    relay_on.setenv("INVINCIBLE_RELAY_KEEP_TURNS", "1")
    messages = _history(3, tool_every=1)
    sent, _ = relay_messages(messages)
    digest = [m for m in sent if m.get("role") == "system"][0]

    assert set(digest.keys()) == {"role", "content"}
    content = digest["content"]
    assert content.startswith("[Context relay]")
    assert 'user: "question 0' in content
    assert "read_file(" in content
    assert "1 tool result(s) elided" in content
    # Atomicity is structural: no tool-call wire fields anywhere in it.
    assert "tool_calls" not in content
    assert "tool_call_id" not in content


def test_tool_arguments_truncated_in_digest(relay_on):
    relay_on.setenv("INVINCIBLE_RELAY_KEEP_TURNS", "1")
    big_args = json.dumps({"path": "a" * 500})
    messages = [
        *_turn(0),
        *_turn(1),
        _assistant_tool_call("big", "edit_file", big_args),
        _tool_result("big", "ok"),
        *_turn(2),
    ]
    sent, _ = relay_messages(messages)
    digest = [m for m in sent if m.get("role") == "system"][0]
    call_line = next(
        line
        for line in digest["content"].splitlines()
        if "edit_file" in line
    )
    assert len(call_line) < 200
    assert "a" * 500 not in call_line


def test_keep_turns_boundary_exact(relay_on):
    relay_on.setenv("INVINCIBLE_RELAY_KEEP_TURNS", "1")
    messages = _history(4)
    sent, stats = relay_messages(messages)
    assert stats.turns_digested == 3
    assert sent[-2:] == messages[-2:]
    assert len(sent) == 3  # digest + the one kept turn (user + assistant)


def test_too_few_turns_never_relayed(relay_on):
    relay_on.setenv("INVINCIBLE_RELAY_KEEP_TURNS", "3")
    messages = _history(2)
    sent, stats = relay_messages(messages)
    assert sent is messages
    assert stats.applied is False


def test_digest_entry_cap(relay_on):
    relay_on.setenv("INVINCIBLE_RELAY_KEEP_TURNS", "1")
    relay_on.setenv("INVINCIBLE_RELAY_DIGEST_MAX_ENTRIES", "5")
    messages = _history(31)
    sent, stats = relay_messages(messages)
    digest = [m for m in sent if m.get("role") == "system"][0]

    assert stats.turns_digested == 30
    assert "(25 earlier turn(s) omitted entirely.)" in digest["content"]
    assert digest["content"].count('- user: "') == 5


def test_system_tokens_do_not_trigger_relay():
    big_system = {"role": "system", "content": "s" * 80000}
    messages = [big_system] + _history(2)
    sent, stats = relay_messages(messages)
    assert sent is messages
    assert stats.applied is False


def test_input_never_mutated(relay_on):
    relay_on.setenv("INVINCIBLE_RELAY_KEEP_TURNS", "1")
    messages = _history(4, tool_every=2)
    before = [json.dumps(m, sort_keys=True) for m in messages]
    sent, _ = relay_messages(messages)
    assert [json.dumps(m, sort_keys=True) for m in messages] == before
    assert sent is not messages
    assert len(messages) == 10  # tool turns carry three messages each


def test_toggle_off_passthrough(monkeypatch):
    monkeypatch.setenv("INVINCIBLE_RELAY_THRESHOLD_TOKENS", "1")
    monkeypatch.setenv("INVINCIBLE_RELAY", "0")
    messages = _history(5)
    sent, stats = relay_messages(messages)
    assert sent is messages
    assert stats == RelayStats()
    assert relay_enabled() is False


def test_never_raises_on_unusual_messages(relay_on):
    relay_on.setenv("INVINCIBLE_RELAY_KEEP_TURNS", "1")
    weird = [
        {"role": "user", "content": None},
        {"role": "assistant", "tool_calls": None},
        {"role": "assistant", "tool_calls": [{"garbage": True}]},
        {"role": "tool", "content": ["block", {"text": "part text"}]},
        {"role": "user", "content": [{"text": "parted"}, {"other": 1}]},
        _user("final question"),
        _assistant("final answer"),
    ]
    sent, stats = relay_messages(weird)
    assert sent[-2:] == weird[-2:]
    assert stats.applied in (True, False)


def test_digest_lands_before_kept_turns_after_systems(relay_on):
    relay_on.setenv("INVINCIBLE_RELAY_KEEP_TURNS", "2")
    messages = [ {"role": "system", "content": "sys"} ] + _history(5)
    sent, _ = relay_messages(messages)
    roles = [m["role"] for m in sent]
    assert roles[0] == "system"
    assert "[Context relay]" in sent[1]["content"]
    assert roles[2] == "user"
