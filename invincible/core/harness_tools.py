# invincible/core/harness_tools.py
"""Single tool registry: metadata source of truth for every surface.

Adding or changing a tool used to mean editing the same tool in many
places that had to stay in sync by hand (``endpoints/mcp.py`` TOOLS +
``_dispatch`` branches, ``agent/runner.py::execute_job`` cases, the
policy gate, audit names, ``core/webchat_agent.py`` schemas + sets +
mode subsets, ``core/harness_router.py`` agent tuples,
``endpoints/docs.py`` plane split). This module is where each tool's
name, descriptions, schemas, and classification are written down ONCE;
every surface derives its copy from here.

Hard rules for this module:

- Metadata only. No execution, no imports of ``endpoints/`` or the
  agent runner (layering: ``core/`` never imports ``endpoints/``).
  Handlers keep their bodies; only their metadata moves here.
- Byte-identity: every derived view reproduces today's wire shapes
  exactly (pinned by ``tests/test_harness_tools_golden.py``), including
  known quirks: ``/mcp`` omits ``required`` when empty while webchat
  emits ``[]``; webchat property schemas carry no ``description`` keys;
  per-surface description wordings differ and both are preserved.
- Fail closed: ``needs_approval`` defaults to True. A new entry that
  does not explicitly opt out requires approval on every surface.
- Views return fresh deep copies: callers can never mutate the
  registry through a view.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from invincible.core.memory import MAX_CONTENT_CHARS, MEMORY_KINDS

# Response caps for the memory/project tools. Single definition: both
# ``endpoints/mcp.py`` and ``core/webchat_agent.py`` import these instead
# of keeping their own copies (same values as before the move).
MEMORY_SEARCH_DEFAULT = 5
MEMORY_SEARCH_MAX = 10
MEMORY_LIST_DEFAULT = 10
MEMORY_LIST_MAX = 20
PROJECT_CAP = 50

WEBCHAT_MODES = ("plan", "manual", "auto")


@dataclass(frozen=True)
class HarnessTool:
    """One tool's metadata, written down exactly once.

    ``properties`` is the ``/mcp`` form (per-property ``description``
    keys included where the MCP descriptor has them); the webchat form
    is derived by :func:`webchat_properties` (descriptions stripped).
    ``required`` is the required-names list; ``/mcp`` omits the key
    when it is empty, webchat always emits it (possibly ``[]``).
    """

    name: str
    description_mcp: str = ""
    description_webchat: str = ""
    properties: dict = field(default_factory=dict)
    required: tuple = ()
    read_only: bool = False
    needs_approval: bool = True
    mcp: bool = False
    webchat: bool = False
    webchat_modes: tuple = ()
    agent_job: str | None = None
    router_agents: tuple = ()
    data_plane: bool = False
    server_executable: bool = True


def _str(**kwargs) -> dict:
    prop: dict = {"type": "string"}
    prop.update(kwargs)
    return prop


def _int(**kwargs) -> dict:
    prop: dict = {"type": "integer"}
    prop.update(kwargs)
    return prop


def _bool(**kwargs) -> dict:
    prop: dict = {"type": "boolean"}
    prop.update(kwargs)
    return prop


TOOLS: tuple[HarnessTool, ...] = (
    HarnessTool(
        name="read_file",
        description_mcp=(
            "Read a file's contents from the host machine. Reads are "
            "sandboxed to the server's working directory and repo root "
            "(extend with INVINCIBLE_READ_ROOTS); files holding secrets or "
            "sensitive state (.env, sessions.db, .git/) are rejected "
            "outright wherever they sit. Results are capped at 65536 "
            "characters and include a truncated flag. No confirmation is "
            "required for other files since reading is non-destructive."
        ),
        description_webchat=(
            "Read a file's contents on the machine that executes tools "
            "(your paired PC when an agent is connected, else the server "
            "host). Secret/state files are rejected. Results are capped "
            "at 65536 characters and include a truncated flag."
        ),
        properties={"path": _str()},
        required=("path",),
        read_only=True,
        needs_approval=False,
        mcp=True,
        webchat=True,
        webchat_modes=("plan", "manual", "auto"),
        agent_job="read_file",
        router_agents=("triage", "operator"),
    ),
    HarnessTool(
        name="execute_bash",
        description_mcp=(
            "Run a shell command on the host machine. Commands matching the "
            "denylist (destructive filesystem ops, privilege escalation, "
            "power commands, etc.) are rejected outright. Everything else "
            "is staged for approval: the call returns a token, and the "
            "command only runs after confirm_action is called with that "
            "token and approve=true."
        ),
        description_webchat=(
            "Run a shell command on the executing machine. Denylisted "
            "commands (destructive fs ops, privilege escalation, power "
            "commands) are rejected outright. Depending on the chat mode "
            "this call either runs immediately or pauses for the user's "
            "approval first."
        ),
        properties={"command": _str()},
        required=("command",),
        read_only=False,
        needs_approval=True,
        mcp=True,
        webchat=True,
        webchat_modes=("manual", "auto"),
        agent_job="execute_bash",
        router_agents=("operator",),
    ),
    HarnessTool(
        name="write_file",
        description_mcp=(
            "Write content to a file on the host machine. Writes to files "
            "this server depends on for its own security or state (.env, "
            "providers.yaml, sessions.db, its own source/tests, .git/) are "
            "rejected outright. Everything else is staged for approval: "
            "the call returns a token, and the file is only written after "
            "confirm_action is called with that token and approve=true."
        ),
        description_webchat=(
            "Write content to a file on the executing machine. "
            "Security/state paths are rejected outright. Depending on the "
            "chat mode this call either runs immediately or pauses for "
            "the user's approval first."
        ),
        properties={"path": _str(), "content": _str()},
        required=("path", "content"),
        read_only=False,
        needs_approval=True,
        mcp=True,
        webchat=True,
        webchat_modes=("manual", "auto"),
        agent_job="write_file",
        router_agents=("operator",),
    ),
    HarnessTool(
        name="code_search",
        description_mcp=(
            "Search files for a text pattern (case-insensitive) under a "
            "directory: ripgrep when installed, a bounded Python walk "
            "otherwise. Same sandbox as read_file (server read roots, or "
            "the agent's home when routed); secret/state files and large "
            "binaries are skipped, results are capped. No confirmation is "
            "required since searching is non-destructive."
        ),
        description_webchat=(
            "Search files for a text pattern under a directory "
            "(case-insensitive, capped results)."
        ),
        properties={
            "pattern": _str(),
            "path": _str(),
            "max_results": _int(),
        },
        required=("pattern", "path"),
        read_only=True,
        needs_approval=False,
        mcp=True,
        webchat=True,
        webchat_modes=("plan", "manual", "auto"),
        agent_job="code_search",
    ),
    HarnessTool(
        name="list_dir",
        description_mcp=(
            "List a directory's entries (names, dir/file kind, file "
            "sizes) on the machine that executes tools — the server "
            "host by default, your own paired machine when agent "
            "routing is on. Same sandbox as read_file (server read "
            "roots, or the agent's home when routed); hidden files "
            "are skipped unless show_hidden is true, entries are "
            "capped. No confirmation is required since listing is "
            "non-destructive."
        ),
        description_webchat=(
            "List a directory's entries (names, kinds, sizes) on the "
            "executing machine. Hidden files skipped unless asked."
        ),
        properties={
            "path": _str(),
            "limit": _int(),
            "show_hidden": _bool(),
        },
        required=("path",),
        read_only=True,
        needs_approval=False,
        mcp=True,
        webchat=True,
        webchat_modes=("plan", "manual", "auto"),
        agent_job="list_dir",
    ),
    HarnessTool(
        name="git_status",
        description_mcp=(
            "Show the git working-tree status (branch, changed files) "
            "for the repository containing a path. Read-only; same "
            "sandbox as read_file. Returns an error (not a block) "
            "when the path is not inside a git repository."
        ),
        description_webchat=(
            "Git working-tree status for the repo containing a path. "
            "Read-only; errors outside a repo."
        ),
        properties={"path": _str()},
        required=("path",),
        read_only=True,
        needs_approval=False,
        mcp=True,
        webchat=True,
        webchat_modes=("plan", "manual", "auto"),
        agent_job="git_status",
    ),
    HarnessTool(
        name="git_diff",
        description_mcp=(
            "Show the unstaged git diff (plus stat summary) for the "
            "repository containing a path. Read-only; same sandbox "
            "as read_file. Diffs over 100KB are truncated with a "
            "flag. Returns an error when the path is not inside a "
            "git repository."
        ),
        description_webchat=(
            "Unstaged git diff (+stat) for the repo containing a path. "
            "Read-only; large diffs truncated."
        ),
        properties={"path": _str()},
        required=("path",),
        read_only=True,
        needs_approval=False,
        mcp=True,
        webchat=True,
        webchat_modes=("plan", "manual", "auto"),
        agent_job="git_diff",
    ),
    HarnessTool(
        name="git_log",
        description_mcp=(
            "List recent commits (hash, author, date, subject), "
            "newest first, for the repository containing a path. "
            "Read-only; same sandbox as read_file. Returns an error "
            "when the path is not inside a git repository."
        ),
        description_webchat=(
            "Recent commits for the repo containing a path, newest "
            "first. Read-only."
        ),
        properties={"path": _str(), "limit": _int()},
        required=("path",),
        read_only=True,
        needs_approval=False,
        mcp=True,
        webchat=True,
        webchat_modes=("plan", "manual", "auto"),
        agent_job="git_log",
    ),
    HarnessTool(
        name="process_list",
        description_mcp=(
            "List running processes (pid, name, cpu/mem where available) "
            "on the machine that executes tools — the server host by "
            "default, your own paired machine when agent routing is on. "
            "Read-only: no confirmation required."
        ),
        description_webchat=(
            "Running processes (pid, name, cpu/mem) on the executing "
            "machine. Read-only."
        ),
        properties={"limit": _int()},
        required=(),
        read_only=True,
        needs_approval=False,
        mcp=True,
        webchat=True,
        webchat_modes=("plan", "manual", "auto"),
        agent_job="process_list",
    ),
    HarnessTool(
        name="screenshot",
        description_mcp=(
            "Capture a headless-Chrome screenshot (1280x800 PNG) of an "
            "http(s) URL for visual validation. Runs ONLY on your paired "
            "machine (agent routing must be on and Chrome/Chromium/Edge "
            "installed) — the server never fetches caller-supplied URLs, "
            "so this path cannot become an SSRF primitive. The agent "
            "finds the browser via INVINCIBLE_CHROME_BIN, PATH, "
            "well-known install paths, and (on Windows) the App-Paths "
            "registry. No confirmation required."
        ),
        description_webchat=(
            "Capture a headless-Chrome screenshot (1280x800 PNG) of an "
            "http(s) URL for visual validation. Runs ONLY on your paired "
            "machine - the server never fetches caller URLs."
        ),
        properties={"url": _str()},
        required=("url",),
        read_only=True,
        needs_approval=False,
        mcp=True,
        webchat=True,
        webchat_modes=("plan", "manual", "auto"),
        agent_job="screenshot",
        server_executable=False,
    ),
    HarnessTool(
        name="confirm_action",
        description_mcp=(
            "Approve or deny a pending execute_bash/write_file request. "
            "Must be called with the exact token returned by that request. "
            "approve=true performs the action immediately (runs the "
            "command / writes the file); approve=false discards it without "
            "executing anything. This is how operator approval is obtained: "
            "an action is never executed until this tool confirms it."
        ),
        properties={"token": _str(), "approve": _bool()},
        required=("token", "approve"),
        read_only=False,
        needs_approval=False,
        mcp=True,
    ),
    HarnessTool(
        name="task_state_set",
        description_mcp=(
            "Persist canonical task progress into Invincible's shared "
            "continuity store for this session. Every later LLM request "
            "(any provider/model) receives this state as its continuation "
            "brief, and later MCP reads return it - one canonical store, "
            "no per-model memory. Payload must be a JSON OBJECT of "
            "structured facts you want preserved verbatim (e.g. "
            '{"task":"count 1-100","completed_through":5,"next_value":6}).'
        ),
        description_webchat=(
            "Persist canonical task progress into the shared continuity "
            "store for this session. Payload must be a JSON OBJECT of "
            "structured facts to preserve verbatim."
        ),
        properties={
            "payload": _str(
                description="JSON object of structured state"),
            "task_key": _str(),
            "status": _str(
                enum=["active", "blocked", "done", "cancelled"]),
            "expected_version": _int(description="optimistic CAS guard"),
            "session_id": _str(),
        },
        required=("payload",),
        read_only=False,
        needs_approval=False,
        mcp=True,
        webchat=True,
        webchat_modes=("manual", "auto"),
        router_agents=("operator",),
        data_plane=True,
    ),
    HarnessTool(
        name="task_state_get",
        description_mcp=(
            "Read the latest trusted task state previously persisted via "
            "task_state_set (or any other writer). Returns "
            "{status,payload,version} or a note when nothing is tracked."
        ),
        description_webchat=(
            "Read the latest trusted task state previously persisted via "
            "task_state_set."
        ),
        properties={"task_key": _str(), "session_id": _str()},
        required=(),
        read_only=True,
        needs_approval=False,
        mcp=True,
        webchat=True,
        webchat_modes=("plan", "manual", "auto"),
        router_agents=("triage", "operator"),
        data_plane=True,
    ),
    HarnessTool(
        name="checkpoint_create",
        description_mcp=(
            "Snapshot the current task-state version as a named checkpoint "
            "(e.g. 'completed through 37'). Checkpoints mark reliable "
            "progress points that survive provider failover and appear in "
            "the session's continuation brief."
        ),
        description_webchat=(
            "Snapshot the current task-state version as a named checkpoint "
            "(e.g. 'completed through 37')."
        ),
        properties={
            "note": _str(), "task_key": _str(), "session_id": _str()},
        required=(),
        read_only=False,
        needs_approval=False,
        mcp=True,
        webchat=True,
        webchat_modes=("manual", "auto"),
        router_agents=("operator",),
        data_plane=True,
    ),
    HarnessTool(
        name="memory_save",
        description_mcp=(
            "Deliberately store a fact about the user or one of their "
            "projects into their memory store - the same store their "
            "dashboard and gateway chats read. Use it for durable facts "
            "worth recalling in later sessions (preferences, decisions, "
            "working context); for transient task progress use "
            "task_state_set instead. No confirmation is required: rows "
            "are user-owned data, reversible from the dashboard."
        ),
        description_webchat=(
            "Deliberately store a fact about the user or one of their "
            "projects into their memory store - the same store dashboard "
            "and gateway chats read. Use for durable facts worth recalling "
            "later; for task progress use task_state_set instead."
        ),
        properties={
            "content": _str(
                description="the fact to remember, "
                            f"1-{MAX_CONTENT_CHARS} characters"),
            "kind": _str(
                enum=list(MEMORY_KINDS),
                description="coarse classifier (default: note)"),
            "project": _str(
                description="one of the user's project names; "
                            "tags the memory to that project "
                            "(default: user-scope)"),
        },
        required=("content",),
        read_only=False,
        needs_approval=False,
        mcp=True,
        webchat=True,
        webchat_modes=("manual", "auto"),
        data_plane=True,
    ),
    HarnessTool(
        name="memory_search",
        description_mcp=(
            "Search the user's memory store with the same ranking their "
            "gateway chats use (lexical relevance x recency x "
            "confidence). Returns a small ranked list, never a dump - "
            "look here before asking the user something you may "
            "already know."
        ),
        description_webchat=(
            "Search the user's memory store with the same ranking gateway "
            "chats use. Returns a small ranked list, never a dump."
        ),
        properties={
            "query": _str(),
            "project": _str(
                description="restrict to that project's "
                            "memories plus user-scope ones"),
            "limit": _int(
                description=f"max results, 1-{MEMORY_SEARCH_MAX} "
                            f"(default {MEMORY_SEARCH_DEFAULT})"),
        },
        required=("query",),
        read_only=True,
        needs_approval=False,
        mcp=True,
        webchat=True,
        webchat_modes=("plan", "manual", "auto"),
        router_agents=("triage",),
        data_plane=True,
    ),
    HarnessTool(
        name="memory_list",
        description_mcp=(
            "Browse the user's most recent memories, newest first - "
            "useful for bootstrapping context at the start of a "
            "session. Optional kind/project filters; capped at "
            f"{MEMORY_LIST_MAX} rows. There is deliberately no "
            "memory_delete over MCP: deletion stays a human, "
            "dashboard-only action."
        ),
        description_webchat=(
            "Browse the user's most recent memories, newest first. "
            "Optional kind/project filters; capped rows. No delete "
            "over chat: deletion stays dashboard-only."
        ),
        properties={
            "limit": _int(
                description=f"max rows, 1-{MEMORY_LIST_MAX} "
                            f"(default {MEMORY_LIST_DEFAULT})"),
            "kind": _str(enum=list(MEMORY_KINDS)),
            "project": _str(
                description="that project's memories plus user-scope ones"),
        },
        required=(),
        read_only=True,
        needs_approval=False,
        mcp=True,
        webchat=True,
        webchat_modes=("plan", "manual", "auto"),
        router_agents=("triage",),
        data_plane=True,
    ),
    HarnessTool(
        name="project_create",
        description_mcp=(
            "Create a new project for the user. Projects scope memories: "
            "a memory saved with project=<name> is only retrieved when "
            "working in that project. Useful when the user starts a "
            "distinct piece of work (\"new project for the blog redesign\") "
            "or when memory_save rejects an unknown project name. Names "
            "are 1-100 characters, unique per user (case-sensitive), and "
            "capped at "
            f"{PROJECT_CAP} projects per user."
        ),
        description_webchat=(
            "Create a new project for the user. Projects scope memories. "
            "Names 1-100 chars, unique per user."
        ),
        properties={
            "name": _str(
                description="the project name, 1-100 characters"),
        },
        required=("name",),
        read_only=False,
        needs_approval=False,
        mcp=True,
        webchat=True,
        webchat_modes=("manual", "auto"),
        data_plane=True,
    ),
    HarnessTool(
        name="project_list",
        description_mcp=(
            "List the user's projects (id, name, is_default, "
            "archived_at). Call this before project-scoped memory_save to "
            "discover valid project names instead of guessing. Archived "
            "projects are hidden unless include_archived=true."
        ),
        description_webchat=(
            "List the user's projects (id, name, is_default). Call before "
            "project-scoped memory_save."
        ),
        properties={
            "include_archived": _bool(
                description="include soft-archived projects (default false)"),
        },
        required=(),
        read_only=True,
        needs_approval=False,
        mcp=True,
        webchat=True,
        webchat_modes=("plan", "manual", "auto"),
        data_plane=True,
    ),
    HarnessTool(
        name="handoff",
        agent_job=None,
        router_agents=("triage",),
        read_only=False,
        needs_approval=False,
    ),
)

_BY_NAME: dict[str, HarnessTool] = {tool.name: tool for tool in TOOLS}

# Webchat offers a different subset order than /mcp lists: names here in
# the exact WEBCHAT_TOOL_SCHEMAS order. A drift test pins this against
# the set of webchat-surface tools, so the two can never diverge.
_WEBCHAT_ORDER: tuple[str, ...] = (
    "read_file",
    "list_dir",
    "code_search",
    "git_status",
    "git_diff",
    "git_log",
    "process_list",
    "execute_bash",
    "write_file",
    "screenshot",
    "memory_save",
    "memory_search",
    "memory_list",
    "project_create",
    "project_list",
    "task_state_set",
    "task_state_get",
    "checkpoint_create",
)


def get_tool(name: str) -> HarnessTool | None:
    """Look up one registry entry (None for unknown names)."""
    return _BY_NAME.get(name)


def webchat_properties(properties: dict) -> dict:
    """Derive the webchat property schemas from the ``/mcp`` form.

    The single named derivation: strip per-property ``description``
    keys, keep everything else (types, enums, structure) identical.
    Golden-pinned byte-for-byte.
    """
    derived = {}
    for key, schema in properties.items():
        if isinstance(schema, dict):
            derived[key] = {
                k: copy.deepcopy(v) for k, v in schema.items()
                if k != "description"
            }
        else:
            derived[key] = copy.deepcopy(schema)
    return derived


def mcp_descriptors() -> list[dict]:
    """``tools/list`` payload, in registry order (== today's order).

    ``required`` is omitted when empty (today's quirk, preserved).
    Fresh deep copies on every call.
    """
    descriptors = []
    for tool in TOOLS:
        if not tool.mcp:
            continue
        schema: dict = {
            "type": "object",
            "properties": copy.deepcopy(tool.properties),
        }
        if tool.required:
            schema["required"] = list(tool.required)
        descriptors.append({
            "name": tool.name,
            "description": tool.description_mcp,
            "inputSchema": schema,
        })
    return descriptors


def webchat_schemas() -> list[dict]:
    """Webchat function schemas, in today's ``WEBCHAT_TOOL_SCHEMAS`` order.

    ``required`` is always emitted (possibly ``[]``). Fresh deep copies.
    """
    schemas = []
    for name in _WEBCHAT_ORDER:
        tool = _BY_NAME[name]
        schemas.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description_webchat,
                "parameters": {
                    "type": "object",
                    "properties": webchat_properties(tool.properties),
                    "required": list(tool.required),
                },
            },
        })
    return schemas


def _webchat_entries() -> list[HarnessTool]:
    """Registry entries with webchat surface, in webchat order."""
    return [_BY_NAME[name] for name in _WEBCHAT_ORDER]


def read_only_names() -> tuple[str, ...]:
    """Today's ``READ_ONLY_TOOLS``: machine reads, no data-plane reads."""
    return tuple(
        tool.name for tool in _webchat_entries()
        if tool.read_only and not tool.data_plane
        and tool.server_executable
    )


def approval_required_names() -> tuple[str, ...]:
    """Today's ``MUTATING_TOOLS``: webchat tools needing approval."""
    return tuple(
        tool.name for tool in _webchat_entries() if tool.needs_approval
    )


def data_read_names() -> tuple[str, ...]:
    """Today's ``DATA_READ_TOOLS``."""
    return tuple(
        tool.name for tool in _webchat_entries()
        if tool.data_plane and tool.read_only
    )


def data_write_names() -> tuple[str, ...]:
    """Today's ``DATA_WRITE_TOOLS``."""
    return tuple(
        tool.name for tool in _webchat_entries()
        if tool.data_plane and not tool.read_only
    )


def agent_only_names() -> tuple[str, ...]:
    """Today's ``AGENT_ONLY_TOOLS``: paired-machine-only execution."""
    return tuple(
        tool.name for tool in _webchat_entries()
        if not tool.server_executable
    )


def webchat_for_mode(mode: str) -> list[dict]:
    """Today's per-mode subsets (plan/manual/auto), in webchat order."""
    names = [
        name for name in _WEBCHAT_ORDER
        if mode in _BY_NAME[name].webchat_modes
    ]
    by_name = {s["function"]["name"]: s for s in webchat_schemas()}
    return [by_name[name] for name in names]


def router_tools(agent_name: str) -> tuple[str, ...]:
    """Today's agent tool tuples (triage/operator), in registry order."""
    return tuple(
        tool.name for tool in TOOLS if agent_name in tool.router_agents)


def docs_data_plane_names() -> frozenset[str]:
    """Today's ``docs._DATA_PLANE_TOOLS`` membership."""
    return frozenset(tool.name for tool in TOOLS if tool.data_plane)
