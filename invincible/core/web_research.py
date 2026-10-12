# invincible/core/web_research.py
"""Server-side web research for the web_research tool (F08, Bluey steal).

Unlike ``web_fetch`` (which pulls arbitrary caller URLs and therefore
runs ONLY on the paired machine), research calls one fixed host: the
user's own provider ``/responses`` endpoint with the web_search tool.
The question is free text, never a URL, and the provider host comes
from the user's stored BYOK credential (SSRF-guarded at connect AND
re-checked here, same as the router's lazy key resolution). Providers
without a ``/responses`` + web_search surface answer 4xx, which maps
to a structured ``unavailable`` result - never an exception.

Layering: pure ``core/`` logic - no FastAPI, no Router import. Both
surfaces (``POST /mcp`` dispatch, webchat ``_execute_call``) call
:func:`run_research` / :func:`first_research_target`.
"""
from __future__ import annotations

import logging
import re

import httpx

from invincible.core import credential_crypto
from invincible.core.credential_store import ByokCredentialStore
from invincible.core.provider_catalog import catalog_entry
from invincible.core.url_safety import UnsafeUrlError, validate_public_https_url

logger = logging.getLogger("invincible.research")

MODEL = "gpt-5.5"
TIMEOUT_SECONDS = 90.0
MAX_PARAGRAPHS = 4
MAX_SOURCES = 5

INSTRUCTIONS = (
    "Research the question on the web and write a short, friendly "
    "report for a busy person. First line: a plain title of under "
    "eight words. Then one to four short paragraphs (four at most), "
    "leading with the direct answer, then the most useful details. "
    "Plain text only: no markdown, no headings, no bullet lists, and "
    "no links or citations in the text."
)


def clean_report_text(text: str) -> list[str]:
    """Strip inline citations/markdown; split into non-empty blocks."""
    text = re.sub(r"\s*\([^)]*\]\([^)]*\)\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\s*\[https?://[^\]]*\]", "", text)
    text = text.replace("**", "").replace("#", "")
    return [
        line.strip()
        for line in text.split("\n")
        if line.strip()
    ]


def parse_report(question: str, payload: dict) -> dict:
    """Shape a ``/responses`` payload into the tool result contract."""
    text = ""
    sources: list[tuple[str, str]] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            text += str(part.get("text") or "")
            for note in part.get("annotations") or []:
                if not isinstance(note, dict):
                    continue
                link = note.get("url")
                if not link or any(url == link for _, url in sources):
                    continue
                sources.append((
                    str(note.get("title") or link),
                    str(link),
                ))
    blocks = clean_report_text(text)
    if not blocks:
        return {"status": "error",
                "error": "Research came back empty; try rephrasing."}
    return {
        "status": "research",
        "question": question,
        "title": blocks[0],
        "paragraphs": blocks[1:1 + MAX_PARAGRAPHS],
        "sources": [
            {"title": title, "url": url}
            for title, url in sources[:MAX_SOURCES]
        ],
    }


async def research(
    question: str, context: str | None, *, base_url: str, api_key: str,
    model: str, transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """Call ``{base_url}/responses`` with the web_search tool."""
    question = (question or "").strip()
    if not question:
        return {"status": "error",
                "error": "Give a question to research."}
    address = base_url.rstrip("/") + "/responses"
    inquiry = question
    if (context or "").strip():
        inquiry += "\n\nWhat's on the user's screen, for context: " + \
            context.strip()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS,
                                     transport=transport) as client:
            response = await client.post(
                address,
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={
                    "model": model or MODEL,
                    "reasoning": {"effort": "low"},
                    "tools": [{"type": "web_search"}],
                    "instructions": INSTRUCTIONS,
                    "input": inquiry,
                },
            )
    except httpx.HTTPError as e:
        logger.warning("web research request failed: %s", e)
        return {"status": "error",
                "error": f"Research request failed: {e}"}
    if response.status_code != 200:
        body = response.text[:160]
        logger.warning("web research HTTP %s: %s",
                       response.status_code, body)
        return {"status": "unavailable",
                "reason": (
                    "This provider does not offer web research "
                    f"({response.status_code} {body})."
                )}
    try:
        payload = response.json()
    except ValueError:
        return {"status": "error",
                "error": "Research came back unreadable; try again."}
    if not isinstance(payload, dict):
        return {"status": "error",
                "error": "Research came back unreadable; try again."}
    return parse_report(question, payload)


async def first_research_target(
    engine, user_id: int,
) -> tuple[str, str, str] | None:
    """First usable ``(base_url, api_key, model)`` in routing order.

    None when the user connected nothing or no credential is usable
    (vanished row, unsafe URL, undecryptable ciphertext) - callers
    render that as an ``unavailable`` result, never a 500.
    """
    if engine is None or user_id is None:
        return None
    store = ByokCredentialStore(engine)
    try:
        rows = await store.routing_rows(user_id)
    except Exception as e:  # noqa: BLE001 - unavailable, not fatal
        logger.warning("web research credential lookup failed: %s", e)
        return None
    for row in rows:
        entry = catalog_entry(row.get("catalog_key"))
        if not (entry and row["base_url"] == entry["base_url"]):
            try:
                validate_public_https_url(row["base_url"])
            except UnsafeUrlError as e:
                logger.warning(
                    "BYOK base URL no longer safe (%s); skipping", e)
                continue
        try:
            key = credential_crypto.decrypt(row["encrypted_api_key"])
        except (credential_crypto.CredentialKeyError,
                credential_crypto.CredentialDecryptError) as e:
            logger.warning("BYOK credential unusable: %s", e)
            continue
        if key:
            return (row["base_url"], key,
                    row.get("model_id") or MODEL)
    return None


async def run_research(engine, user_id: int, args: dict) -> dict:
    """Tool entry: validate, resolve target, research. Never raises."""
    question = str((args or {}).get("question", "") or "").strip()
    if not question:
        return {"status": "error",
                "error": "Give a question to research."}
    target = await first_research_target(engine, user_id)
    if target is None:
        return {"status": "unavailable",
                "reason": (
                    "Web research needs a connected provider: add one "
                    "under /dashboard/providers first."
                )}
    base_url, api_key, model = target
    context = str((args or {}).get("context", "") or "")
    return await research(question, context, base_url=base_url,
                          api_key=api_key, model=model)
