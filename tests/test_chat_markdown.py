# tests/test_chat_markdown.py
"""Webchat markdown bubbles: server-rendered, raw HTML disabled."""
from invincible.core.markdown import render_chat_markdown
from invincible.endpoints.chat import _renderable_turns


def test_bold_and_code():
    html = render_chat_markdown("**bold** and `code`")
    assert "<strong>bold</strong>" in html
    assert "<code>code</code>" in html


def test_lists_codefence_table():
    html = render_chat_markdown(
        "- a\n- b\n\n```python\nprint(1)\n```\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n"
    )
    assert "<ul>" in html
    assert 'class="language-python"' in html
    assert "<table>" in html


def test_raw_html_escaped():
    html = render_chat_markdown("<script>alert(1)</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_img_onerror_escaped():
    html = render_chat_markdown('<img src=x onerror="alert(1)">')
    assert "<img" not in html


def test_empty():
    assert render_chat_markdown("") == ""
    assert render_chat_markdown("   ") == ""


def test_renderable_turns_assistant_html_user_plain():
    turns = _renderable_turns([
        {"role": "user", "content": "**not md**"},
        {"role": "assistant", "content": "**bold**"},
        {"role": "system", "content": "skip me"},
    ])
    assert turns[0] == {"role": "user", "text": "**not md**"}
    assert turns[1]["role"] == "assistant"
    assert turns[1]["text"] == "**bold**"
    assert "<strong>bold</strong>" in turns[1]["html"]
