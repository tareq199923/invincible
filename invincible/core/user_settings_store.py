# invincible/core/user_settings_store.py
"""Per-user routing mode + request-shaping overrides (Phase 1 self-service).

Thin store over ``user_settings``, same discipline as ByokCredentialStore:
ownership goes through ``user_id`` on every read/write, and the request
path NEVER trusts the stored JSON - :func:`routing_config_from_user`
degrades any malformed row to auto routing instead of raising.

The routing JSON mirrors the operator registry's shape but references the
user's own ``user_provider_credentials`` ids::

    {"mode": "chain", "chain": [{"credential_id": 2,
                                 "model": "moonshotai/kimi-k3"}, ...]}
    {"mode": "pinned", "pinned": {"credential_id": 4,
                                  "model": "gemini-3.6-flash"}}
    {"mode": "auto"}

``overrides`` keys (memory, continuity, compression, relay,
history_max_turns) each mean "fall through to the env default" when
absent - server-level secrets and config stay env-only by construction.
"""
import time

from sqlalchemy.dialects.postgresql import insert as pg_insert

from invincible.core.db import user_settings
from invincible.core.selection import (
    AUTO_ROUTING,
    PinnedRoute,
    RoutingConfig,
    chain_with_model_hint,
)


class UnknownOverrideKeyError(Exception):
    """Save-time validation: an overrides key outside the known set."""


# The request-shaping toggles a user may override. Server-level secrets
# and config are env-only by construction - anything not listed here is
# rejected at save time.
OVERRIDABLE_KEYS = (
    "memory",
    "continuity",
    "compression",
    "relay",
    "history_max_turns",
)


def override_flag(overrides: dict | None, key: str, env_enabled) -> bool:
    """Boolean toggle with per-user override: a stored ``key`` wins for
    that user, a missing key falls through to the server default
    (``env_enabled`` - a live-read settings accessor, so tests can flip
    it). Shared by the Router pipeline gates and the endpoints'
    injection gates so both sides read the same rule."""
    if overrides is not None and key in overrides:
        return bool(overrides[key])
    return env_enabled()


def override_int(overrides: dict | None, key: str, env_default) -> int | None:
    """Integer-valued override (history_max_turns): a stored positive int
    wins, a stored ``0``/``off`` disables (None), anything else falls
    through to the env accessor's value."""
    if overrides is not None and key in overrides:
        value = overrides[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return env_default()
        value = int(value)
        if value <= 0:
            return None
        return value
    return env_default()


def clean_overrides(raw: object) -> dict:
    """Save-time validation of a submitted overrides mapping: known keys
    only, booleans coerced, ``history_max_turns`` a non-negative int
    (0 = no cap). Raises ValueError naming the problem; the endpoint
    turns that into a 400."""
    if not isinstance(raw, dict):
        raise ValueError("Overrides must be a mapping")
    cleaned = {}
    for key, value in raw.items():
        if key not in OVERRIDABLE_KEYS:
            raise UnknownOverrideKeyError(f"Unknown setting '{key}'")
        if key == "history_max_turns":
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(
                    "history_max_turns must be a whole number (0 = no cap)"
                )
            cleaned[key] = value
        else:
            cleaned[key] = bool(value)
    return cleaned


class UserSettingsStore:
    def __init__(self, engine):
        self.engine = engine

    async def get(self, user_id: int) -> dict:
        """The user's stored ``{"routing": ..., "overrides": ...}`` row,
        or empty dicts when no row exists (the pre-Phase-1 default)."""
        async with self.engine.connect() as conn:
            row = (await conn.execute(
                user_settings.select()
                .where(user_settings.c.user_id == user_id)
            )).mappings().first()
        if row is None:
            return {"routing": {}, "overrides": {}}
        return {
            "routing": dict(row["routing"] or {}),
            "overrides": dict(row["overrides"] or {}),
        }

    async def routing_for(self, user_id: int) -> dict:
        """The stored routing JSON alone ({} = auto)."""
        return (await self.get(user_id))["routing"]

    async def overrides_for(self, user_id: int) -> dict:
        """The stored overrides JSON alone ({} = follow env defaults)."""
        return (await self.get(user_id))["overrides"]

    async def save_routing(self, user_id: int, routing: dict) -> None:
        """Upsert the routing JSON (validated by the caller at save time;
        the request path re-degrades defensively)."""
        await self._upsert(user_id, routing=routing)

    async def save_overrides(self, user_id: int, overrides: dict) -> None:
        """Upsert the overrides JSON."""
        await self._upsert(user_id, overrides=overrides)

    async def _upsert(self, user_id: int, **columns) -> None:
        now = time.time()
        values = {"user_id": user_id, "updated_at": now, **columns}
        async with self.engine.begin() as conn:
            await conn.execute(
                pg_insert(user_settings)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["user_id"],
                    set_={**columns, "updated_at": now},
                )
            )


def routing_config_from_user(
    routing_json: object,
    credential_rows: list[dict],
    model: str | None = None,
) -> RoutingConfig:
    """Map a user's stored routing JSON onto their credential rows.

    Chain/pinned steps carry credential ids, but the selection layer (and
    the shared ``attempt_order``) matches candidates by ``name`` - each
    credential row's unique ``provider_name`` - so the mapping happens
    here. Runtime drift (a step whose credential was deleted) silently
    drops that step, the same semantics the operator registry's chain
    keeps (selection.py).

    ``model`` (the client's requested model) only reorders a chain: the
    matching step floats to the front so the entry point follows the
    client's choice, then the rest follow in the user's order as
    cross-model fallbacks. It never rewrites a step's own model.

    Any malformed shape degrades to ``AUTO_ROUTING`` - a corrupt row must
    never take a request down.
    """
    if not isinstance(routing_json, dict):
        return AUTO_ROUTING
    mode = routing_json.get("mode", "auto")

    def _route(step) -> PinnedRoute | None:
        """One JSON step -> PinnedRoute over the credential's name, or
        None when the step is malformed or drifted off the user's rows."""
        if not isinstance(step, dict):
            return None
        credential_id = step.get("credential_id")
        model_id = step.get("model")
        if not isinstance(credential_id, int) or not (
            isinstance(model_id, str) and model_id.strip()
        ):
            return None
        row = next(
            (r for r in credential_rows if r["id"] == credential_id), None
        )
        if row is None:
            return None
        return PinnedRoute(provider=row["provider_name"], model=model_id)

    if mode == "pinned":
        pinned = _route(routing_json.get("pinned"))
        if pinned is None:
            return AUTO_ROUTING
        return RoutingConfig(mode="pinned", pinned=pinned)

    if mode == "chain":
        chain_json = routing_json.get("chain")
        if not isinstance(chain_json, list):
            return AUTO_ROUTING
        # Hint float first (raw steps carry the models), then map to
        # routes - drifting steps stay dropped wherever they sat.
        chain_json = chain_with_model_hint(chain_json, model)
        chain = tuple(
            route for route in (_route(step) for step in chain_json)
            if route is not None
        )
        if not chain:
            return AUTO_ROUTING
        return RoutingConfig(mode="chain", chain=chain)

    return AUTO_ROUTING
