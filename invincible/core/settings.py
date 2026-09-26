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

# Agent routing (core/agent_registry.py + endpoints/agents.py). An agent
# counts as online if it polled within this window; a long-poll holds this
# long before answering "nothing"; dispatched jobs get the action's own
# timeout plus this grace before the holding /mcp request gives up.
AGENT_ONLINE_TTL_SECONDS = 60
AGENT_POLL_HOLD_SECONDS = 25
AGENT_JOB_GRACE_SECONDS = 10

# Lexical retrieval caps (core/retrieval.py, Phase 4).
DEFAULT_MEMORY_TOP_N = 8
DEFAULT_MEMORY_MIN_SCORE = 0.01

# Shared injection budget (core/context_builder.py, Phase 4): total tokens
# for memory + continuity injections combined. Conservative enough that a
# provider with a ~4k-token context still has room for history.
DEFAULT_INJECTION_BUDGET_TOKENS = 1200

# Stored-history turn cap when INVINCIBLE_HISTORY_MAX_TURNS is unset.
DEFAULT_HISTORY_MAX_TURNS = 200

# Tool-schema compression caps (core/tool_compression.py): tool-level and
# property-level description truncation, in characters.
DEFAULT_TOOL_DESCRIPTION_MAX_CHARS = 512
DEFAULT_TOOL_PROPERTY_DESCRIPTION_MAX_CHARS = 160

# Context relay (core/relay.py): relay engages only above this estimated
# token size, keeps the newest N turns verbatim, and caps how many digested
# turns get individual digest entries (older ones collapse to a count line).
DEFAULT_RELAY_THRESHOLD_TOKENS = 12000
DEFAULT_RELAY_KEEP_TURNS = 3
DEFAULT_RELAY_DIGEST_MAX_ENTRIES = 20

# Off-switch vocabulary shared by every INVINCIBLE_* boolean toggle.
_OFF_VALUES = ("0", "false", "off")


def _env_flag(name: str) -> bool:
    """The INVINCIBLE_* toggle convention: unset (or anything but the off
    values, case-insensitive) means enabled."""
    return os.getenv(name, "").strip().lower() not in _OFF_VALUES


class Settings:
    """Live-read accessors for every environment variable the app owns."""

    def db_url(self) -> str | None:
        """PostgreSQL DSN (INVINCIBLE_DB_URL). Required since Phase 16;
        e.g. postgresql+asyncpg://invincible:pw@localhost:5433/invincible"""
        return os.getenv("INVINCIBLE_DB_URL")

    def config_path(self) -> str | None:
        """Explicit providers.yaml override (INVINCIBLE_CONFIG_PATH)."""
        return os.getenv("INVINCIBLE_CONFIG_PATH")

    def credential_key(self) -> str | None:
        """Master key encrypting user BYOK provider credentials at rest
        (INVINCIBLE_CREDENTIAL_KEY). Unset (or malformed) refuses every
        /providers/mine surface - fail closed, the same posture the
        management API keeps toward INVINCIBLE_OWNER_SECRET: stored user
        keys are never plaintext.
        """
        return os.getenv("INVINCIBLE_CREDENTIAL_KEY")

    def owner_secret(self) -> str | None:
        """HMAC key source signing account browser sessions
        (core.accounts SessionManager). The OAuth owner-secret LOGIN was
        removed in Phase 2 - consent is a logged-in account session - but
        the sessions themselves still fail closed when this is unset.
        """
        return os.getenv("INVINCIBLE_OWNER_SECRET")

    def github_client_id(self) -> str | None:
        """GitHub OAuth App client ID - unset hides GitHub login entirely."""
        return os.getenv("INVINCIBLE_GITHUB_CLIENT_ID")

    def github_client_secret(self) -> str | None:
        """GitHub OAuth App client secret (never logged, never returned)."""
        return os.getenv("INVINCIBLE_GITHUB_CLIENT_SECRET")

    def persist_pending_actions(self) -> bool:
        """Whether staged MCP actions survive a restart."""
        return bool(os.getenv("INVINCIBLE_PERSIST_PENDING_ACTIONS"))

    def agent_routing(self) -> bool:
        """Route confirmed MCP tool execution to the caller's paired
        agent (Phase 10). Opt-in on purpose, unlike the default-on
        INVINCIBLE_* feature toggles: unset means every tool executes
        locally on the server host, exactly as before, so local
        ``invincible start`` development workflows are untouched.
        Public multi-user deploys MUST set INVINCIBLE_AGENT_ROUTING=1 (see
        docs/SECURITY.md §10 deployment posture)."""
        return bool(os.getenv("INVINCIBLE_AGENT_ROUTING"))

    def harness_ws_enabled(self) -> bool:
        """Agent WebSocket relay (H1). Default on: WS-first with long-poll
        fallback. Set INVINCIBLE_HARNESS_WS=0/off/false to force poll-only
        (useful behind proxies that break WS)."""
        return _env_flag("INVINCIBLE_HARNESS_WS")

    def harness_ws_heartbeat_seconds(self) -> float:
        """WS keepalive pings from the agent (H1)."""
        try:
            return max(
                1.0, float(os.getenv("INVINCIBLE_HARNESS_WS_HEARTBEAT", ""))
            )
        except ValueError:
            return 20.0

    def harness_summarizer_enabled(self) -> bool:
        """Opt-in LLM summarizer for harness context compaction (H3).
        DEFAULT OFF (explicit allowlist, like the debug toggles): each
        compaction burns an upstream call on the caller's BYOK credentials,
        so the relay digest in core/relay.py stays the default path."""
        return os.getenv(
            "INVINCIBLE_HARNESS_SUMMARIZER", "").strip().lower() in (
            "1", "true", "on", "yes",
        )

    def debug_dump_400(self) -> bool:
        """Opt-in: dump the exact outgoing payload on non-failover 400s to
        debug_400_<provider>_<epoch>.json. DEFAULT OFF (explicit allowlist,
        unlike the opt-out INVINCIBLE_* feature toggles) - dumps contain
        conversation content and are gitignored."""
        return os.getenv("INVINCIBLE_DEBUG_400", "").strip().lower() in (
            "1", "true", "on", "yes",
        )

    def debug_stream(self) -> bool:
        """Opt-in: capture every upstream SSE chunk (plus the outgoing
        payload and the assembled assistant turn) for one request into
        debug_stream_<request_id>.json. DEFAULT OFF - like
        :meth:`debug_dump_400` this is an explicit allowlist because the
        dumps contain conversation content and are gitignored. Unset means
        the capture path is never taken and nothing is written."""
        return os.getenv("INVINCIBLE_DEBUG_STREAM", "").strip().lower() in (
            "1", "true", "on", "yes",
        )

    def compression_enabled(self) -> bool:
        """Send-time request compression (default on)."""
        return _env_flag("INVINCIBLE_COMPRESSION")

    def tool_compression_enabled(self) -> bool:
        """Send-time tool-schema compression (default on)."""
        return _env_flag("INVINCIBLE_TOOL_COMPRESSION")

    def tool_description_max_chars(self) -> int:
        """Cap for tool-level ``function.description`` length."""
        try:
            return max(
                1,
                int(os.getenv("INVINCIBLE_TOOL_DESCRIPTION_MAX_CHARS", "")),
            )
        except ValueError:
            return DEFAULT_TOOL_DESCRIPTION_MAX_CHARS

    def tool_property_description_max_chars(self) -> int:
        """Cap for property-level descriptions inside tool ``parameters``."""
        try:
            return max(
                1,
                int(
                    os.getenv(
                        "INVINCIBLE_TOOL_PROPERTY_DESCRIPTION_MAX_CHARS", ""
                    )
                ),
            )
        except ValueError:
            return DEFAULT_TOOL_PROPERTY_DESCRIPTION_MAX_CHARS

    def relay_enabled(self) -> bool:
        """Context relay: digest old turns into one system message (default
        on)."""
        return _env_flag("INVINCIBLE_RELAY")

    def relay_threshold_tokens(self) -> int:
        """Estimated-token floor above which relay engages."""
        try:
            return max(
                0, int(os.getenv("INVINCIBLE_RELAY_THRESHOLD_TOKENS", ""))
            )
        except ValueError:
            return DEFAULT_RELAY_THRESHOLD_TOKENS

    def relay_keep_turns(self) -> int:
        """Newest turns relay always leaves verbatim."""
        try:
            return max(1, int(os.getenv("INVINCIBLE_RELAY_KEEP_TURNS", "")))
        except ValueError:
            return DEFAULT_RELAY_KEEP_TURNS

    def relay_digest_max_entries(self) -> int:
        """Max digested turns that get individual digest entries; older
        ones collapse into a single count line."""
        try:
            return max(
                1, int(os.getenv("INVINCIBLE_RELAY_DIGEST_MAX_ENTRIES", ""))
            )
        except ValueError:
            return DEFAULT_RELAY_DIGEST_MAX_ENTRIES

    def memory_enabled(self) -> bool:
        """Fact extraction/injection (default on)."""
        return _env_flag("INVINCIBLE_MEMORY")

    def memory_explicit_enabled(self) -> bool:
        """Explicit \"remember this\" / \"save this\" capture into scoped
        memories (Phase 4, default on). Independent of INVINCIBLE_MEMORY so
        auto-extraction can be silenced while deliberate saves still land."""
        return _env_flag("INVINCIBLE_MEMORY_EXPLICIT")

    def continuity_enabled(self) -> bool:
        """Continuation-brief injection from the ContinuityEngine (15b,
        default on). State WRITES are unaffected by this toggle - it only
        gates rendering into outgoing prompts."""
        return _env_flag("INVINCIBLE_CONTINUITY")

    def memory_top_n(self) -> int:
        """Max memories injected per request by RetrievalService."""
        try:
            return max(0, int(os.getenv("INVINCIBLE_MEMORY_TOP_N", "")))
        except ValueError:
            return DEFAULT_MEMORY_TOP_N

    def memory_min_score(self) -> float:
        """Relevance floor: scored memories below this are not injected."""
        try:
            return max(0.0, float(os.getenv("INVINCIBLE_MEMORY_MIN_SCORE", "")))
        except ValueError:
            return DEFAULT_MEMORY_MIN_SCORE

    def injection_budget_tokens(self) -> int:
        """Unified token budget for memory + continuity injections."""
        try:
            return max(
                0, int(os.getenv("INVINCIBLE_INJECTION_BUDGET_TOKENS", ""))
            )
        except ValueError:
            return DEFAULT_INJECTION_BUDGET_TOKENS

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

    def chrome_bin(self) -> str | None:
        """Explicit browser binary for agent-side screenshots
        (INVINCIBLE_CHROME_BIN). Unset means auto-discovery (PATH, then
        OS well-known install paths, then the Windows App-Paths
        registry). Set it on a paired machine whose browser lives
        somewhere unusual, then restart ``harness connect``."""
        raw = os.getenv("INVINCIBLE_CHROME_BIN", "").strip()
        return raw or None


settings = Settings()
