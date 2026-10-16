# invincible/core/markdown.py
"""Server-side chat markdown rendering (webchat assistant bubbles).

Single source for assistant-bubble HTML: history turns and the
``done`` SSE event both go through :func:`render_chat_markdown` so a
reloaded page and a just-streamed turn can never disagree.

Security posture: raw HTML is disabled (``html: False``), so model
output like ``<script>`` or ``<img onerror=...>`` renders as escaped
text, never markup. The caller inserts the result via ``innerHTML``
exactly like the old escaped+``<br>`` bubble — no new XSS surface.
"""
from markdown_it import MarkdownIt

# Bound: a single bubble never needs more than this; caps render cost
# on pathological model output.
_MAX_CHARS = 50000

_md: MarkdownIt | None = None


def _get_md() -> MarkdownIt:
    global _md
    if _md is None:
        _md = MarkdownIt("commonmark", {"html": False}).enable("table")
    return _md


def render_chat_markdown(text: str) -> str:
    """Render assistant plain-text into safe bubble HTML."""
    if not isinstance(text, str) or not text.strip():
        return ""
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS]
    return _get_md().render(text)
