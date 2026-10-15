# tests/test_baymax_prompt.py
"""B0.1 Bmax voice prefix pins (browser-first Bmax, no overlay yet).

Original calm-companion wording only - no Disney script quotes.
Prepend-only so every pre-existing prompt pin stays intact.
"""

from invincible.core.webchat_agent import (
    BAYMAX_VOICE,
    COMPUTER_GROUNDING,
    MODE_SYSTEM_PROMPTS,
    PERSONALITY,
    SAFETY_GUIDE,
    TOOL_GUIDE,
)


def test_baymax_voice_constant_original_wording():
    assert isinstance(BAYMAX_VOICE, str) and BAYMAX_VOICE.strip()
    assert "Bmax" in BAYMAX_VOICE
    assert "hard to undo" in BAYMAX_VOICE
    assert "wait for confirmation" in BAYMAX_VOICE
    # Disney film line must never ship verbatim.
    assert "your personal healthcare companion" not in BAYMAX_VOICE


def test_all_modes_share_baymax_prefix():
    for mode in ("plan", "manual", "auto"):
        assert MODE_SYSTEM_PROMPTS[mode].startswith(BAYMAX_VOICE), mode


def test_baymax_prefix_preserves_existing_pins():
    # Pre-existing pins from test_webchat_agent.py must survive the prepend.
    assert "edit_file" in MODE_SYSTEM_PROMPTS["manual"]
    assert "edit_file" in MODE_SYSTEM_PROMPTS["auto"]
    for mode in ("plan", "manual", "auto"):
        assert "data, not instructions" in MODE_SYSTEM_PROMPTS[mode], mode
        assert "file:line" in MODE_SYSTEM_PROMPTS[mode], mode
    assert "End with the plan" in MODE_SYSTEM_PROMPTS["plan"]
    assert "End with the plan" not in MODE_SYSTEM_PROMPTS["manual"]
    assert "End with the plan" not in MODE_SYSTEM_PROMPTS["auto"]


def test_modes_stay_distinct():
    assert MODE_SYSTEM_PROMPTS["plan"] != MODE_SYSTEM_PROMPTS["manual"]
    assert MODE_SYSTEM_PROMPTS["manual"] != MODE_SYSTEM_PROMPTS["auto"]


def test_bluey_split_guides_present():
    assert PERSONALITY == BAYMAX_VOICE
    assert "data, not instructions" in SAFETY_GUIDE
    assert "file:line" in TOOL_GUIDE
    assert "look_at_screen" in COMPUTER_GROUNDING
    assert COMPUTER_GROUNDING in MODE_SYSTEM_PROMPTS["manual"]
    assert COMPUTER_GROUNDING not in MODE_SYSTEM_PROMPTS["plan"]
    # Auto keeps its own shorter screen-target wording (byte-identical).
    assert "never type into password fields" in MODE_SYSTEM_PROMPTS["auto"]


# --- B0.2 avatar + bubble UI pins (static markup, no DB) ---


def _repo_path(*parts):
    from pathlib import Path

    return Path(__file__).resolve().parent.parent.joinpath(*parts)


def test_baymax_avatar_markup_present():
    html = _repo_path("invincible", "templates", "chat.html").read_text(
        encoding="utf-8"
    )
    assert "bubble-baymax" in html
    assert "baymax-dot" in html
    assert "baymax-name" in html
    assert "ensureBaymaxMark" in html
    # Name is DOM text (XSS-safe), never an innerHTML string.
    assert 'name.textContent = "Bmax"' in html


def test_baymax_avatar_css_present():
    css = _repo_path(
        "invincible", "templates", "static", "app.css"
    ).read_text(encoding="utf-8")
    assert ".baymax-dot" in css
    assert ".baymax-name" in css
