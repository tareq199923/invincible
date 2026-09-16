# invincible/core/principal.py
"""Authenticated principal model.

A Principal is whatever presented a valid credential on this request:

- ``api_key`` - a per-user ``inv_`` API key (hashed at rest, shown once);
- ``session`` - a logged-in dashboard account session.

Ownership predicates on every query path resolve against
``user_id``; there is no shared or operator identity anymore.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    user_id: int
    project_id: int
    kind: str
    api_key_id: int | None = None
