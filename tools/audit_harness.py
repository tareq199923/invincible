"""Harness audit: score this repo's five agent-harness subsystems.

Step B of the repo's own harness notebook (feature_list.json F06).
Zero dependencies — stdlib only, so any contributor can run it:

    python tools/audit_harness.py [path/to/repo]

Exit 0 when every CRITICAL check passes, 1 otherwise. RECOMMENDED
failures never change the exit code; they print as WARN with a fix.

What it reads (and only this): AGENTS.md/CLAUDE.md, PROGRESS.md,
feature_list.json, and the *names* of dependency manifests. It never
opens .env files, credentials, or conversation content.

Conventions honored: JSON columns bind objects natively elsewhere in
this repo; here JSON is only *read* (json.load), never written.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

VALID_STATES = ("planned", "active", "passing")
REQUIRED_ENTRY_FIELDS = ("id", "behavior", "verification", "state")

# Dependency manifests that make installs reproducible, by ecosystem.
LOCK_SIGNALS = (
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Pipfile.lock",
    "requirements.txt",
    "Cargo.lock",
    "go.sum",
    "Gemfile.lock",
    "pyproject.toml",
    "package.json",
)

VERIFICATION_HINT = re.compile(
    r"(make check|make test|npm test|pytest|cargo test|go test|"
    r"mix test|yarn test|verification|verify)",
    re.IGNORECASE,
)


@dataclass
class Finding:
    subsystem: str
    name: str
    level: str  # "CRITICAL" or "RECOMMENDED"
    passed: bool
    fix: str = ""


@dataclass
class AuditResult:
    findings: list[Finding] = field(default_factory=list)

    @property
    def critical_failed(self) -> int:
        return sum(
            1
            for f in self.findings
            if f.level == "CRITICAL" and not f.passed
        )

    @property
    def critical_passed(self) -> int:
        return sum(
            1 for f in self.findings if f.level == "CRITICAL" and f.passed
        )

    @property
    def recommended_failed(self) -> int:
        return sum(
            1
            for f in self.findings
            if f.level == "RECOMMENDED" and not f.passed
        )

    @property
    def recommended_passed(self) -> int:
        return sum(
            1 for f in self.findings if f.level == "RECOMMENDED" and f.passed
        )

    @property
    def ok(self) -> bool:
        return self.critical_failed == 0


def _instructions_file(root: Path) -> Path | None:
    for name in ("AGENTS.md", "CLAUDE.md"):
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def audit_repo(root: Path | str) -> AuditResult:
    """Run every check against *root* and return the findings."""
    root = Path(root)
    result = AuditResult()

    def check(
        subsystem: str,
        name: str,
        level: str,
        passed: bool,
        fix: str = "",
    ) -> None:
        result.findings.append(
            Finding(subsystem, name, level, bool(passed), fix)
        )

    # ── Subsystem 1: Instructions ──────────────────────────────
    instructions = _instructions_file(root)
    check(
        "Instructions",
        "AGENTS.md or CLAUDE.md exists at repo root",
        "CRITICAL",
        instructions is not None,
        "Create AGENTS.md at the repo root with system description, "
        "verification command, and state-file references.",
    )
    if instructions is not None:
        text = instructions.read_text(encoding="utf-8", errors="replace")
        check(
            "Instructions",
            "Verification command is documented",
            "CRITICAL",
            VERIFICATION_HINT.search(text) is not None,
            f"Add a Verification section to {instructions.name} naming the "
            "command (e.g. `ruff check .`, `pytest`).",
        )
        check(
            "Instructions",
            "Clock-in/out routine is documented",
            "RECOMMENDED",
            re.search(
                r"(clock-in|clock-out|session protocol)",
                text,
                re.IGNORECASE,
            )
            is not None,
            f"Add a Session protocol section to {instructions.name}: what to "
            "read at start, what to update at end.",
        )
        check(
            "Instructions",
            "Hard constraints (MUST / MUST NOT) are stated",
            "RECOMMENDED",
            re.search(r"\bMUST\b|\bMUST NOT\b|\bnever\b", text) is not None,
            f"Add explicit MUST / MUST NOT rules to {instructions.name}.",
        )
    else:
        check(
            "Instructions",
            "Verification command is documented",
            "CRITICAL",
            False,
            "Create AGENTS.md first (see previous finding).",
        )

    # ── Subsystem 2: Environment ───────────────────────────────
    check(
        "Environment",
        "Dependency manifest present (reproducible installs)",
        "CRITICAL",
        any((root / name).exists() for name in LOCK_SIGNALS),
        "Commit a manifest/lockfile (pyproject.toml, package-lock.json, "
        "requirements.txt, go.sum, ...).",
    )

    # ── Subsystem 3: State ─────────────────────────────────────
    progress = root / "PROGRESS.md"
    check(
        "State",
        "PROGRESS.md exists at repo root",
        "CRITICAL",
        progress.is_file(),
        "Create PROGRESS.md with Current State, In Progress, Next Steps, "
        "Blockers.",
    )
    if progress.is_file():
        body = progress.read_text(encoding="utf-8", errors="replace")
        check(
            "State",
            "PROGRESS.md has Current State + Next Steps blocks",
            "RECOMMENDED",
            re.search(r"current state", body, re.IGNORECASE) is not None
            and re.search(r"next steps?", body, re.IGNORECASE) is not None,
            "Add ## Current State and ## Next Steps to PROGRESS.md.",
        )

    feature_file = root / "feature_list.json"
    entries: list[dict] = []
    feature_valid = False
    if feature_file.is_file():
        try:
            data = json.loads(
                feature_file.read_text(encoding="utf-8")
            )
            entries = data if isinstance(data, list) else []
            feature_valid = bool(entries) and all(
                isinstance(e, dict)
                and all(k in e for k in REQUIRED_ENTRY_FIELDS)
                for e in entries
            )
        except (json.JSONDecodeError, UnicodeDecodeError):
            feature_valid = False
    check(
        "State",
        "feature_list.json exists with id/behavior/verification/state",
        "CRITICAL",
        feature_valid,
        "Create feature_list.json: a list of objects each with id, "
        'behavior, verification, state ("planned"/"active"/"passing").',
    )
    if feature_valid:
        states = [e["state"] for e in entries]
        check(
            "State",
            "WIP=1: at most one feature is active",
            "RECOMMENDED",
            states.count("active") <= 1,
            "Move all but one feature out of `active` — finish it to "
            "`passing` before activating the next.",
        )
        check(
            "State",
            "All states are planned/active/passing",
            "RECOMMENDED",
            all(s in VALID_STATES for s in states),
            "Fix unknown state values; the machine is "
            "not_started-free by design: planned → active → passing.",
        )

    # ── Subsystem 4: Lifecycle ─────────────────────────────────
    check(
        "Lifecycle",
        "Clean-state checklist exists",
        "RECOMMENDED",
        (root / "templates" / "clean-state-checklist.md").is_file(),
        "Create templates/clean-state-checklist.md with the 5 clock-out "
        "dimensions (build, tests, feature list, debug artifacts, "
        "PROGRESS.md).",
    )

    return result


def render(result: AuditResult, root: Path) -> str:
    lines = [f"Harness audit: {root}", ""]
    current_section = ""
    for finding in result.findings:
        if finding.subsystem != current_section:
            current_section = finding.subsystem
            lines.append(f"== {current_section} ==")
        mark = (
            "PASS"
            if finding.passed
            else ("FAIL" if finding.level == "CRITICAL" else "WARN")
        )
        lines.append(f"  [{mark}] [{finding.level}] {finding.name}")
        if not finding.passed and finding.fix:
            lines.append(f"         fix: {finding.fix}")
    lines += [
        "",
        f"CRITICAL {result.critical_passed}/"
        f"{result.critical_passed + result.critical_failed} passed; "
        f"RECOMMENDED {result.recommended_passed}/"
        f"{result.recommended_passed + result.recommended_failed} passed.",
        "All CRITICAL checks pass."
        if result.ok
        else "CRITICAL items missing — fix these before long agent sessions.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit this repo's agent-harness subsystems."
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=".",
        help="Repo root to audit (default: current directory).",
    )
    args = parser.parse_args(argv)
    root = Path(args.target)
    result = audit_repo(root)
    print(render(result, root))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
