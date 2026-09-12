# invincible/models/responses.py
"""Pydantic model for the OpenAI Responses API request surface.

Only the fields Invincible understands are declared. Every other Responses
field (``reasoning``, ``include``, ``text``, ``temperature``, ``store``,
``previous_response_id``, ``parallel_tool_calls``, ``metadata``, …) and any
unknown future field is ignored rather than rejected, so Codex (or any
Responses client) never receives a 422 for a feature Invincible doesn't
act on. ``store`` in particular is ignored by design: the gateway is
stateless across requests and the session store is keyed by client
headers, not by server-side response ids.
"""
from typing import Any

from pydantic import BaseModel, ConfigDict


class ResponsesRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str | None = None
    instructions: str | None = None
    input: str | list[dict[str, Any]]
    stream: bool | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: dict[str, Any] | str | None = None
