# invincible/core/principal.py
"""Authenticated principal model (Platform Phase 1).

A Principal is whatever presented a valid credential on this request:

- ``legacy``   - the shared ``GATEWAY_API_KEY`` (local mode), mapped to
  the system *local* owner;
- ``api_key``  - a per-user API key (hashed at rest, shown once);
- ``anonymous`` - the fail-open local identity when no gateway key is
  configured (unchanged single-tenant behavior, loud startup warning).

Phase 2 layers ownership predicates on every query path on top of these
identities; the legacy/anonymous realms keep working indefinitely in
local mode (deprecated for hosted mode only, Phase 8).
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    user_id: int
    project_id: int
    kind: str
    api_key_id: int | None = None

    @property
    def is_local(self) -> bool:
        """True when this principal is the system local owner (either the
        legacy shared key or the fail-open anonymous identity)."""
        return self.kind in ("legacy", "anonymous")
