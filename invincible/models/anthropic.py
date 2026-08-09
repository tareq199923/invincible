# invincible/models/anthropic.py
"""Pydantic model for the Anthropic Messages API request surface.

Only the fields Invincible understands are declared. Every other Anthropic
field (``tools``, ``tool_choice``, ``metadata``, ``temperature``, ``top_p``,
``top_k``, ``stop_sequences``, …) and any unknown future field is ignored
rather than rejected, so Claude Code never receives a 422 for an optional
feature Invincible doesn't act on. ``anthropic-beta`` / ``anthropic-version``
are headers and pass through the HTTP layer untouched.
"""
from typing import Any

from pydantic import BaseModel, ConfigDict


class AnthropicMessagesRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str | None = None
    messages: list[dict[str, Any]]
    system: str | list[dict[str, Any]] | None = None
    max_tokens: int | None = None
    stream: bool | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: dict[str, Any] | str | None = None
