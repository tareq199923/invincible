# invincible/migrations/__init__.py
"""Packaged Alembic migration environment (Phase 16).

Ships inside the wheel (like providers.yaml) so `invincible db upgrade`
works from any cwd and any install mode. The schema source of truth is
``invincible.core.db.metadata``; revisions must never drift from it.
"""
