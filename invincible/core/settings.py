# invincible/core/settings.py
"""Central typed configuration surface for the running application
(Phase 13).

Every environment read performed inside the app - lifespan, auth, Router,
stores, compression/memory toggles, tool sandboxing - funnels through the
module-level ``settings`` instance, so variable names, parsing rules, and
defaults exist in exactly one place. ``invincible/cli.py`` is a documented
exemption: it acts as launcher/checker (writes .env, exports process env
before importing the app, checks key presence dynamically) rather than as
part of the running service.

Design: **live reads, deliberately.** Accessors call ``os.getenv`` on every
call instead of snapshotting at import or startup. The CLI start command
exports ``INVINCIBLE_*`` variables immediately before lazily importing the
FastAPI app, and tests flip toggles via monkeypatch between requests;
either would break against an eager snapshot. Phase 16 (PostgreSQL) is
expected to add explicitly constructed snapshots for DB URL/pool settings.
"""
import os

# --- Tuning constants owned here so defaults live in one place ------------

# Provider failure cooldown curve (core/provider_health.py): base seconds,
# doubling per consecutive failure, capped.
COOLDOWN_BASE_SECONDS = 30
COOLDOWN_CAP_SECONDS = 300

# Staged MCP actions expire this many seconds after creation unless
# confirmed (PendingActionStore.TTL_SECONDS sources its default here).
PENDING_ACTION_TTL_SECONDS = 600

# Fact-injection cap (core/memory.py) when INVINCIBLE_MEMORY_MAX_FACTS is
# unset or unparseable.
DEFAULT_MEMORY_MAX_FACTS = 40

# Stored-history turn cap when INVINCIBLE_HISTORY_MAX_TURNS is unset.
DEFAULT_HISTORY_MAX_TURNS = 200

# Off-switch vocabulary shared by every INVINCIBLE_* boolean toggle.
_OFF_VALUES = ("0", "false", "off")


def _env_flag(name: str) -> bool:
    """The INVINCIBLE_* toggle convention: unset (or anything but the off
    values, case-insensitive) means enabled."""
    return os.getenv(name, "").strip().lower() not in _OFF_VALUES


class Settings:
    """Live-read accessors for every environment variable the app owns."""

    def gateway_api_key(self) -> str | None:
        """Bearer/x-api-key credential guarding /v1/* (unset = fail open)."""
        return os.getenv("GATEWAY_API_KEY")

    def db_url(self) -> str | None:
        """PostgreSQL DSN (INVINCIBLE_DB_URL). Required since Phase 16;
        e.g. postgresql+asyncpg://invincible:pw@localhost:5433/invincible"""
        return os.getenv("INVINCIBLE_DB_URL")

    def db_path(self) -> str | None:
        """Legacy SQLite path - retained ONLY as input for
        ``invincible db import``. The server never opens SQLite files."""
        return os.getenv("INVINCIBLE_DB_PATH")

    def config_path(self) -> str | None:
        """Explicit providers.yaml override (INVINCIBLE_CONFIG_PATH)."""
        return os.getenv("INVINCIBLE_CONFIG_PATH")

    def admin_key(self) -> str | None:
        """Management-API credential (INVINCIBLE_ADMIN_KEY).

        Deliberately separate from GATEWAY_API_KEY: chat clients must never
        be able to mutate provider configuration. Unset disables the whole
        management surface (fail closed).
        """
        return os.getenv("INVINCIBLE_ADMIN_KEY")

    def providers_file(self) -> str | None:
        """Writable provider-registry file (INVINCIBLE_PROVIDERS_FILE).

        Unset = packaged YAML loaded read-only; management mutations refuse
        until an operator points this at a real path.
        """
        return os.getenv("INVINCIBLE_PROVIDERS_FILE")

    def persist_pending_actions(self) -> bool:
        """Whether staged MCP actions survive a restart."""
        return bool(os.getenv("INVINCIBLE_PERSIST_PENDING_ACTIONS"))

    def debug_dump_400(self) -> bool:
        """Opt-in: dump the exact outgoing payload on non-failover 400s to
        debug_400_<provider>_<epoch>.json. DEFAULT OFF (explicit allowlist,
        unlike the opt-out INVINCIBLE_* feature toggles) - dumps contain
        conversation content and are gitignored."""
        return os.getenv("INVINCIBLE_DEBUG_400", "").strip().lower() in (
            "1", "true", "on", "yes",
        )

    def compression_enabled(self) -> bool:
        """Send-time request compression (default on)."""
        return _env_flag("INVINCIBLE_COMPRESSION")

    def memory_enabled(self) -> bool:
        """Fact extraction/injection (default on)."""
        return _env_flag("INVINCIBLE_MEMORY")

    def continuity_enabled(self) -> bool:
        """Continuation-brief injection from the ContinuityEngine (15b,
        default on). State WRITES are unaffected by this toggle - it only
        gates rendering into outgoing prompts."""
        return _env_flag("INVINCIBLE_CONTINUITY")

    def memory_max_facts(self) -> int:
        """Injection cap for the fact summary system message."""
        try:
            return max(1, int(os.getenv("INVINCIBLE_MEMORY_MAX_FACTS", "")))
        except ValueError:
            return DEFAULT_MEMORY_MAX_FACTS

    def history_max_turns(self) -> int | None:
        """Stored-history turn cap; ``0``/``off`` disables the cap."""
        raw = os.getenv("INVINCIBLE_HISTORY_MAX_TURNS", "").strip().lower()
        if raw in _OFF_VALUES:
            return None
        try:
            return max(1, int(raw)) if raw else DEFAULT_HISTORY_MAX_TURNS
        except ValueError:
            return DEFAULT_HISTORY_MAX_TURNS

    def read_roots(self) -> list[str]:
        """Extra read_file sandbox roots, os.pathsep-separated, stripped."""
        extra = os.getenv("INVINCIBLE_READ_ROOTS", "")
        return [entry.strip() for entry in extra.split(os.pathsep) if entry.strip()]

    def provider_api_key(self, api_key_env: str) -> str | None:
        """Resolve one provider's API key via its configured env-var name."""
        return os.getenv(api_key_env)


settings = Settings()
