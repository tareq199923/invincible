"""Unit tests for send-time tool-schema compression (core/tool_compression.py).

Hermetic: pure functions on plain dicts, no fixtures, no Postgres.
"""
import json

import pytest

from invincible.core.tool_compression import (
    ToolCompressionStats,
    _compress_cached,
    compress_tools,
    tool_compression_enabled,
)

LONG_DESC = "You are a file reader. " * 40  # ~920 chars
LONG_PROP_DESC = "The path to read. " * 20  # ~360 chars
TRUNCATION_MARKER = "…[description truncated]"


def _tool(name="read_file", description=None, parameters=None):
    function = {"name": name}
    if description is not None:
        function["description"] = description
    if parameters is not None:
        function["parameters"] = parameters
    return {"type": "function", "function": function}


def _snapshot(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def test_long_tool_description_truncated():
    tools = [_tool(description=LONG_DESC)]
    sent, stats = compress_tools(tools)
    description = sent[0]["function"]["description"]
    assert description == LONG_DESC[:512] + TRUNCATION_MARKER
    assert stats.descriptions_truncated == 1


def test_property_descriptions_truncated_recursively():
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": LONG_PROP_DESC},
        },
        "items": {"type": "string", "description": LONG_PROP_DESC},
        "anyOf": [{"type": "string", "description": LONG_PROP_DESC}],
        "$defs": {"sub": {"type": "string", "description": LONG_PROP_DESC}},
    }
    sent, stats = compress_tools(
        [_tool(parameters=parameters)]
    )
    function = sent[0]["function"]
    for node in (
        function["parameters"]["properties"]["path"],
        function["parameters"]["items"],
        function["parameters"]["anyOf"][0],
        function["parameters"]["$defs"]["sub"],
    ):
        assert node["description"] == LONG_PROP_DESC[:160] + TRUNCATION_MARKER
    assert stats.descriptions_truncated == 4


def test_semantic_keys_untouched():
    parameters = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["a", "b"], "default": "a"},
        },
        "required": ["mode"],
        "additionalProperties": False,
    }
    sent, _ = compress_tools([_tool(parameters=parameters)])
    sent_parameters = sent[0]["function"]["parameters"]
    assert sent_parameters == parameters
    assert sent[0]["function"]["name"] == "read_file"


def test_strips_exactly_the_allowlisted_keys():
    parameters = {
        "title": "Params",
        "examples": [{"mode": "a"}],
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:x",
        "$comment": "internal",
        "type": "object",
        "format": "uri",
        "default": {},
        "properties": {
            "mode": {"type": "string", "title": "Mode", "$comment": "c"},
        },
    }
    sent, stats = compress_tools([_tool(parameters=parameters)])
    sent_parameters = sent[0]["function"]["parameters"]
    assert "title" not in sent_parameters
    assert "examples" not in sent_parameters
    assert "$schema" not in sent_parameters
    assert "$id" not in sent_parameters
    assert "$comment" not in sent_parameters
    # Validation-affecting keys survive.
    assert sent_parameters["type"] == "object"
    assert sent_parameters["format"] == "uri"
    assert sent_parameters["default"] == {}
    # One nested strip inside properties.mode.
    assert stats.keys_stripped == 7


def test_blank_runs_collapsed_in_kept_descriptions():
    description = "Line one.\n\n\n\n\nLine two."
    sent, _ = compress_tools([_tool(description=description)])
    assert sent[0]["function"]["description"] == "Line one.\n\nLine two."


def test_input_never_mutated():
    tools = [
        _tool(
            description=LONG_DESC,
            parameters={"title": "T", "properties": {}},
        )
    ]
    before = _snapshot(tools)
    sent, _ = compress_tools(tools)
    assert _snapshot(tools) == before
    assert sent is not tools
    assert sent[0] is not tools[0]


def test_toggle_off_passthrough(monkeypatch):
    monkeypatch.setenv("INVINCIBLE_TOOL_COMPRESSION", "0")
    tools = [_tool(description=LONG_DESC, parameters={"title": "T"})]
    sent, stats = compress_tools(tools)
    assert sent is tools
    assert stats.cache_hit is False
    assert stats.descriptions_truncated == 0
    assert stats.keys_stripped == 0
    assert stats.tools_before_bytes == stats.tools_after_bytes
    assert not tool_compression_enabled()


def test_malformed_entries_pass_through():
    tools = ["not-a-dict", 42, {"no": "function"}, None]
    before = _snapshot(tools)
    sent, stats = compress_tools(tools)
    assert sent[:2] == ["not-a-dict", 42]
    assert sent[2] == {"no": "function"}
    assert sent[3] is None
    assert stats.descriptions_truncated == 0
    assert _snapshot(tools) == before


def test_cache_hit_on_identical_second_call():
    _compress_cached.cache_clear()
    tools = [_tool(name="cache_probe", description=LONG_DESC)]
    first, first_stats = compress_tools(tools)
    assert first_stats.cache_hit is False
    second, second_stats = compress_tools(tools)
    assert second_stats.cache_hit is True
    assert second == first
    assert second_stats.tools_after_bytes == first_stats.tools_after_bytes
    assert second_stats.descriptions_truncated == 1


def test_unserializable_input_returned_untouched():
    tools = [{"type": "function", "function": {"name": "x", "odd": object()}}]
    sent, stats = compress_tools(tools)
    assert sent is tools
    assert stats.cache_hit is False


def test_stats_bytes_arithmetic():
    tools = [_tool(description=LONG_DESC, parameters={"title": "T"})]
    sent, stats = compress_tools(tools)
    assert stats.tools_before_bytes == len(json.dumps(tools, ensure_ascii=False))
    assert stats.tools_after_bytes == len(json.dumps(sent, ensure_ascii=False))
    assert stats.tools_after_bytes < stats.tools_before_bytes
    assert stats.keys_stripped == 1


def test_env_caps_respected_and_cache_key_includes_them(monkeypatch):
    _compress_cached.cache_clear()
    tools = [_tool(name="caps_probe", description=LONG_DESC)]
    sent, _ = compress_tools(tools)
    assert len(sent[0]["function"]["description"]) == 512 + len(TRUNCATION_MARKER)

    monkeypatch.setenv("INVINCIBLE_TOOL_DESCRIPTION_MAX_CHARS", "20")
    tighter, _ = compress_tools(tools)
    assert tighter[0]["function"]["description"] == LONG_DESC[:20] + TRUNCATION_MARKER

    monkeypatch.setenv("INVINCIBLE_TOOL_PROPERTY_DESCRIPTION_MAX_CHARS", "5")
    sent_prop, stats = compress_tools(
        [_tool(name="caps_probe2", parameters={
            "properties": {"p": {"type": "string", "description": LONG_PROP_DESC}},
        })]
    )
    assert sent_prop[0]["function"]["parameters"]["properties"]["p"][
        "description"
    ] == LONG_PROP_DESC[:5] + TRUNCATION_MARKER
    assert stats.descriptions_truncated == 1


def test_empty_tools_passthrough():
    assert compress_tools(None) == (None, ToolCompressionStats())
    sent, stats = compress_tools([])
    assert sent == []
    assert stats == ToolCompressionStats()


def test_cache_respects_cap_change_for_same_payload(monkeypatch):
    _compress_cached.cache_clear()
    tools = [_tool(name="cap_change_probe", description=LONG_DESC)]
    _, wide = compress_tools(tools)
    monkeypatch.setenv("INVINCIBLE_TOOL_DESCRIPTION_MAX_CHARS", "30")
    _, tight = compress_tools(tools)
    assert tight.cache_hit is False
    assert tight.tools_after_bytes < wide.tools_after_bytes


@pytest.mark.parametrize(
    "flag", ["0", "false", "OFF", "Off"]
)
def test_toggle_off_values(monkeypatch, flag):
    monkeypatch.setenv("INVINCIBLE_TOOL_COMPRESSION", flag)
    assert tool_compression_enabled() is False
