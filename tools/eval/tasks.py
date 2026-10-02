"""Task schema: load + validate ``tools/eval/tasks/*.yaml``.

Fields: id, category, tags, prompt, files (rel path -> content),
checks (list), max_tool_calls (optional), timeout_seconds (=180).
Unknown keys fail loudly. Pure: PyYAML + stdlib only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CATEGORIES = ("read", "write", "safety", "memory")

_CHECK_TYPES = (
    "file_exists",
    "file_absent",
    "file_contains",
    "file_unchanged",
    "final_text_contains",
    "final_text_not_contains",
    "tool_called",
    "tool_not_called",
    "tool_call_count_max",
    "shell_check",
    "sentinel_survives",
)

_TASK_KEYS = frozenset({
    "id", "category", "tags", "prompt", "files",
    "checks", "max_tool_calls", "timeout_seconds",
})

_CHECK_KEYS = frozenset({
    "type", "path", "substring", "pattern", "command",
    "tool", "max", "hash_of",
})

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass
class EvalTask:
    id: str
    category: str
    tags: list[str] = field(default_factory=list)
    prompt: str = ""
    files: dict[str, str] = field(default_factory=dict)
    checks: list[dict] = field(default_factory=list)
    max_tool_calls: int | None = None
    timeout_seconds: int = 180


def _fail(what: str) -> ValueError:
    return ValueError(what)


def validate_task_dict(raw: dict, source: str = "<dict>") -> EvalTask:
    """Validate one parsed YAML mapping, return an :class:`EvalTask`."""
    if not isinstance(raw, dict):
        raise _fail(f"{source}: task must be a mapping")
    unknown = set(raw) - _TASK_KEYS
    if unknown:
        raise _fail(f"{source}: unknown keys: {sorted(unknown)}")
    task_id = raw.get("id")
    if not isinstance(task_id, str) or not _ID_RE.match(task_id):
        raise _fail(f"{source}: 'id' must be slug-like [a-z0-9_-]")
    category = raw.get("category")
    if category not in CATEGORIES:
        raise _fail(f"{source}: 'category' must be one of {CATEGORIES}")
    tags = raw.get("tags", [])
    if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
        raise _fail(f"{source}: 'tags' must be a list of strings")
    prompt = raw.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise _fail(f"{source}: 'prompt' must be a non-empty string")
    files = raw.get("files", {})
    if not isinstance(files, dict):
        raise _fail(f"{source}: 'files' must be a mapping of rel-path -> content")
    for rel, content in files.items():
        if not isinstance(rel, str) or not isinstance(content, str):
            raise _fail(f"{source}: 'files' entries must be str -> str")
        if rel.startswith(("/", "\\")) or ".." in Path(rel).parts:
            raise _fail(f"{source}: 'files' path escapes task dir: {rel!r}")
    checks = raw.get("checks")
    if not isinstance(checks, list) or not checks:
        raise _fail(f"{source}: 'checks' must be a non-empty list")
    for i, check in enumerate(checks):
        _validate_check(check, f"{source} checks[{i}]")
    max_calls = raw.get("max_tool_calls")
    if max_calls is not None and (
        not isinstance(max_calls, int) or max_calls < 0
    ):
        raise _fail(f"{source}: 'max_tool_calls' must be a non-negative int")
    timeout = raw.get("timeout_seconds", 180)
    if not isinstance(timeout, int) or timeout <= 0:
        raise _fail(f"{source}: 'timeout_seconds' must be a positive int")
    return EvalTask(
        id=task_id,
        category=category,
        tags=list(tags),
        prompt=prompt,
        files=dict(files),
        checks=[dict(c) for c in checks],
        max_tool_calls=max_calls,
        timeout_seconds=timeout,
    )


def _validate_check(check: object, where: str) -> None:
    if not isinstance(check, dict):
        raise _fail(f"{where}: check must be a mapping")
    unknown = set(check) - _CHECK_KEYS
    if unknown:
        raise _fail(f"{where}: unknown keys: {sorted(unknown)}")
    ctype = check.get("type")
    if ctype not in _CHECK_TYPES:
        raise _fail(f"{where}: unknown check type {ctype!r}")
    if ctype in ("file_exists", "file_absent", "file_unchanged"):
        if not isinstance(check.get("path"), str):
            raise _fail(f"{where}: '{ctype}' needs string 'path'")
    elif ctype == "file_contains":
        if not isinstance(check.get("path"), str):
            raise _fail(f"{where}: 'file_contains' needs string 'path'")
        if not isinstance(check.get("substring", ""), str) and not isinstance(
            check.get("pattern", ""), str
        ):
            raise _fail(f"{where}: 'file_contains' needs 'substring' or 'pattern'")
        if "substring" not in check and "pattern" not in check:
            raise _fail(f"{where}: 'file_contains' needs 'substring' or 'pattern'")
    elif ctype in ("final_text_contains", "final_text_not_contains"):
        if not isinstance(check.get("pattern"), str):
            raise _fail(f"{where}: '{ctype}' needs string 'pattern'")
        try:
            re.compile(check["pattern"])
        except re.error as e:
            raise _fail(f"{where}: bad regex: {e}") from None
    elif ctype in ("tool_called", "tool_not_called"):
        if not isinstance(check.get("tool"), str):
            raise _fail(f"{where}: '{ctype}' needs string 'tool'")
    elif ctype == "tool_call_count_max":
        if not isinstance(check.get("max"), int) or check["max"] < 0:
            raise _fail(f"{where}: 'tool_call_count_max' needs non-negative 'max'")
    elif ctype == "shell_check" and (
        not isinstance(check.get("command"), str) or not check["command"].strip()
    ):
        raise _fail(f"{where}: 'shell_check' needs non-empty 'command'")
    elif ctype == "sentinel_survives":
        pass  # no fields; runner checks the sibling sentinel file survived


def load_tasks(directory: str | Path) -> list[EvalTask]:
    """Load every ``*.yaml`` in ``directory`` (sorted), validate each."""
    directory = Path(directory)
    if not directory.is_dir():
        raise ValueError(f"task dir not found: {directory}")
    tasks: list[EvalTask] = []
    seen: set[str] = set()
    for path in sorted(directory.glob("*.yaml")):
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        task = validate_task_dict(raw, source=str(path))
        if task.id in seen:
            raise ValueError(f"duplicate task id: {task.id}")
        seen.add(task.id)
        tasks.append(task)
    return tasks


def filter_tasks(
    tasks: list[EvalTask],
    *,
    task_id: str | None = None,
    category: str | None = None,
    include_memory: bool = False,
) -> list[EvalTask]:
    """Apply CLI filters. Memory-tagged tasks need ``include_memory``."""
    out = list(tasks)
    if not include_memory:
        out = [t for t in out if "memory" not in t.tags and t.category != "memory"]
    if category is not None:
        out = [t for t in out if t.category == category]
    if task_id is not None:
        out = [t for t in out if t.id == task_id]
    return out
