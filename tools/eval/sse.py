"""Incremental SSE parser for the dashboard webchat stream.

Wire format (``core/chat_service.py::web_sse_event``)::

    event: <name>
    data: <json>

    <blank line>

Handles events split across HTTP chunks, multi-line ``data:`` fields
(joined with ``\\n``), and ignores ``:`` comment / keep-alive lines.
No network, no dependencies beyond stdlib.
"""

from __future__ import annotations

import json


class SseParser:
    """Feed raw stream bytes/str, pull out ``(event, data)`` pairs."""

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, chunk: bytes | str) -> list[tuple[str, dict]]:
        """Append ``chunk``, return newly completed events.

        ``data`` is ``json.loads``-parsed when possible; unparseable
        payloads arrive as ``{"_raw": text}`` so graders never crash.
        """
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", errors="replace")
        self._buf += chunk
        events: list[tuple[str, dict]] = []
        while True:
            sep = self._buf.find("\n\n")
            if sep < 0:
                break
            raw = self._buf[:sep]
            self._buf = self._buf[sep + 2:]
            parsed = self._parse_block(raw)
            if parsed is not None:
                events.append(parsed)
        return events

    def flush(self) -> list[tuple[str, dict]]:
        """Return any trailing complete event still buffered."""
        events: list[tuple[str, dict]] = []
        if self._buf.strip():
            parsed = self._parse_block(self._buf)
            if parsed is not None:
                events.append(parsed)
        self._buf = ""
        return events

    @staticmethod
    def _parse_block(block: str) -> tuple[str, dict] | None:
        name: str | None = None
        data_lines: list[str] = []
        for line in block.replace("\r\n", "\n").split("\n"):
            if not line:
                continue
            if line.startswith(":"):
                continue  # comment / keep-alive
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].lstrip(" "))
            # Unknown fields (id:, retry:) are ignored deliberately.
        if name is None and not data_lines:
            return None
        raw = "\n".join(data_lines)
        try:
            data = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, ValueError):
            data = {"_raw": raw}
        if not isinstance(data, dict):
            data = {"_value": data}
        return (name or "message", data)
