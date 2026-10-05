"""Deterministic fixture generation for eval tasks.

Pure + seeded: only ``random.Random(seed).random()`` (plus small helpers
built on it — other ``Random`` methods are not stability-guaranteed across
Python versions). No clock, no locale, no ``hash()``.
All files are written with LF endings so Windows and Linux output is
byte-identical.

``materialize_fixture()`` is the ONE shared entry point used by both
``tools/eval/runner.py`` and the hermetic tests, so live runs and
fairness proofs build byte-identical workspaces.
"""

from __future__ import annotations

import random
from pathlib import Path

YAML_SIZE_CAP = 300 * 1024


def _below(rng: random.Random, n: int) -> int:
    """Uniform int in ``[0, n)`` built only on ``rng.random()``."""
    return int(rng.random() * n)


def _pick(rng: random.Random, seq: list) -> object:
    return seq[_below(rng, len(seq))]


def _req_id(rng: random.Random, taken: set[str]) -> str:
    for _ in range(1000):
        candidate = f"{_below(rng, 65536):04x}"
        if candidate not in taken:
            taken.add(candidate)
            return candidate
    raise ValueError("req id space exhausted")


# --- retry_logs ---------------------------------------------------------------
# Kind ``retry_logs``: per-node service logs for the many-files-read task.
# Event attribution needs a three-hop join on purpose: attempt and result
# live on different lines joined by req id, node->service only in the file
# header, and the retry delay only on a service-keyed policy line.

_NOISE_HOSTS = ["edge-01", "edge-02", "core-01", "core-02"]
_NOISE_OPS = ["forward", "relay", "store", "flush"]
_NOISE_BYTES = [64, 128, 512, 1024, 1500]
_NOISE_STATUS = ["sent", "queued", "acked"]

_FILL_RESULTS = ["ok", "ok", "error"]


def _noise_line(rng: random.Random) -> str:
    return (
        f"h={_pick(rng, _NOISE_HOSTS)} op={_pick(rng, _NOISE_OPS)} "
        f"bytes={_pick(rng, _NOISE_BYTES)} status={_pick(rng, _NOISE_STATUS)}"
    )


def validate_retry_logs_facts(facts: object, paths: list[str]) -> None:
    """Semantic check for ``retry_logs`` facts (structural checks live in
    ``tasks.py``). Raises ``ValueError`` on any problem."""
    if not isinstance(facts, dict):
        raise ValueError("retry_logs 'facts' must be a mapping")
    unknown = set(facts) - {
        "nodes", "services", "true_event", "policy", "decoy_policy", "decoys",
    }
    if unknown:
        raise ValueError(f"retry_logs facts unknown keys: {sorted(unknown)}")
    nodes = facts.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        raise ValueError("retry_logs facts need a non-empty 'nodes' mapping")
    services = facts.get("services")
    if not isinstance(services, dict) or set(services) != set(nodes):
        raise ValueError(
            "retry_logs facts need 'services' mapping every node to a service")
    path_set = set(paths)
    for node, file in nodes.items():
        if file not in path_set:
            raise ValueError(f"retry_logs node {node!r} file not in paths")
    true = facts.get("true_event")
    for key in ("file", "req", "node", "attempt", "result", "approx_line"):
        if not isinstance(true, dict) or key not in true:
            raise ValueError(f"retry_logs true_event needs {key!r}")
    if true["file"] not in path_set:
        raise ValueError("retry_logs true_event file not in paths")
    if true["attempt"] != 3 or true["result"] != "timeout":
        raise ValueError("retry_logs true_event must be attempt 3 + timeout")
    policy = facts.get("policy")
    for key in ("file", "service", "delay_ms", "approx_line"):
        if not isinstance(policy, dict) or key not in policy:
            raise ValueError(f"retry_logs policy needs {key!r}")
    if policy["file"] not in path_set:
        raise ValueError("retry_logs policy file not in paths")
    decoys = facts.get("decoys", [])
    if not isinstance(decoys, list) or not decoys:
        raise ValueError("retry_logs facts need a non-empty 'decoys' list")
    for decoy in decoys:
        for key in ("file", "req", "node", "attempt", "result", "approx_line"):
            if not isinstance(decoy, dict) or key not in decoy:
                raise ValueError(f"retry_logs decoy needs {key!r}")
            if decoy["file"] not in path_set:
                raise ValueError("retry_logs decoy file not in paths")
        if decoy["attempt"] == 3 and decoy["result"] == "timeout":
            raise ValueError("retry_logs decoy must not be attempt 3 + timeout")
        if decoy["req"] == true["req"]:
            raise ValueError("retry_logs decoy reuses the true req id")
    decoy_policy = facts.get("decoy_policy")
    if decoy_policy is not None:
        for key in ("file", "service", "delay_ms"):
            if not isinstance(decoy_policy, dict) or key not in decoy_policy:
                raise ValueError(f"retry_logs decoy_policy needs {key!r}")
        if decoy_policy["delay_ms"] == policy["delay_ms"]:
            raise ValueError("retry_logs decoy delay must differ from true delay")


def _slot_pairs(
    rng: random.Random,
    taken: set[str],
    node: str,
    n_timeout: int,
    n_third: int,
    n_misc: int,
) -> list[tuple[str, int, str]]:
    """Filler (req, attempt, result) slots. attempt==3 never pairs with
    timeout; timeout never pairs with attempt 3 — only the planted true
    event is (3, timeout)."""
    slots: list[tuple[str, int, str]] = []
    for _ in range(n_timeout):
        slots.append((
            _req_id(rng, taken), _pick(rng, [1, 2]), "timeout"))
    for _ in range(n_third):
        slots.append((
            _req_id(rng, taken), 3, _pick(rng, _FILL_RESULTS)))
    for _ in range(n_misc):
        slots.append((
            _req_id(rng, taken), _pick(rng, [1, 2]),
            _pick(rng, ["ok", "ok", "error"])))
    # Deterministic Fisher-Yates built only on rng.random().
    for i in range(len(slots) - 1, 0, -1):
        j = _below(rng, i + 1)
        slots[i], slots[j] = slots[j], slots[i]
    return slots


def build_retry_logs(spec: dict) -> dict[str, bytes]:
    """Build ``{rel-path: bytes}`` for a ``retry_logs`` spec."""
    seed = spec["seed"]
    size_bytes = spec["size_bytes"]
    facts = spec["facts"]
    paths: list[str] = list(spec["paths"])
    nodes: dict[str, str] = dict(facts["nodes"])
    services: dict[str, str] = dict(facts["services"])
    file_to_node = {file: node for node, file in nodes.items()}

    rng = random.Random(seed)
    taken: set[str] = set()
    for decoy in facts.get("decoys", []):
        taken.add(decoy["req"])
    taken.add(facts["true_event"]["req"])

    true = facts["true_event"]
    policy = facts["policy"]
    decoy_policy = facts.get("decoy_policy")
    decoys_by_file: dict[str, list[dict]] = {}
    for decoy in facts.get("decoys", []):
        decoys_by_file.setdefault(decoy["file"], []).append(decoy)

    out: dict[str, bytes] = {}
    for rel in paths:
        node = file_to_node[rel]
        service = services[node]
        lines = [f"node: {node} service: {service}", "log start"]
        if rel == policy["file"]:
            lines.append(
                f"policy service={policy['service']} "
                f"retry_delay_ms={policy['delay_ms']}")
            if decoy_policy is not None:
                lines.append(
                    f"policy service={decoy_policy['service']} "
                    f"retry_delay_ms={decoy_policy['delay_ms']}")

        slots = _slot_pairs(rng, taken, node, 3, 3, 2)
        for decoy in decoys_by_file.get(rel, []):
            slots.append((decoy["req"], decoy["attempt"], decoy["result"]))
        is_true_file = rel == true["file"]
        true_at = true["approx_line"] if is_true_file else -1

        body: list[str] = []
        for req, attempt, result in slots:
            gap = 3 + _below(rng, 4)
            body.append(f"req={req} node={node} attempt={attempt}")
            body.extend(_noise_line(rng) for _ in range(gap))
            body.append(f"req={req} result={result}")
            body.extend(_noise_line(rng) for _ in range(1 + _below(rng, 3)))
        if is_true_file:
            gap = 3 + _below(rng, 4)
            pair = [
                f"req={true['req']} node={true['node']} "
                f"attempt={true['attempt']}",
                *(_noise_line(rng) for _ in range(gap)),
                f"req={true['req']} result={true['result']}",
            ]
            at = max(0, min(true_at, len(body)))
            body[at:at] = pair

        lines.extend(body)
        while len("\n".join(lines).encode("utf-8")) + 1 < size_bytes:
            lines.append(_noise_line(rng))
        out[rel] = ("\n".join(lines) + "\n").encode("utf-8")
    return out


_BUILDERS = {"retry_logs": build_retry_logs}

_FACT_VALIDATORS = {"retry_logs": validate_retry_logs_facts}


def validate_generate_spec(spec: object, static_paths: set[str]) -> None:
    """Deep validation for one ``generate:`` entry. Raises ``ValueError``."""
    if not isinstance(spec, dict):
        raise ValueError("generate entry must be a mapping")
    unknown = set(spec) - {"kind", "paths", "seed", "size_bytes", "facts"}
    if unknown:
        raise ValueError(f"generate entry unknown keys: {sorted(unknown)}")
    kind = spec.get("kind")
    if kind not in _BUILDERS:
        raise ValueError(f"generate unknown kind {kind!r}")
    paths = spec.get("paths")
    if (not isinstance(paths, list) or not paths
            or any(not isinstance(p, str) for p in paths)):
        raise ValueError("generate 'paths' must be a non-empty list of strings")
    for rel in paths:
        if rel.startswith(("/", "\\")) or ".." in Path(rel).parts:
            raise ValueError(f"generate path escapes task dir: {rel!r}")
    if set(paths) & static_paths:
        raise ValueError("generate paths collide with static files")
    if not isinstance(spec.get("seed"), int):
        raise ValueError("generate 'seed' must be an int")
    size = spec.get("size_bytes")
    if not isinstance(size, int) or size <= 0:
        raise ValueError("generate 'size_bytes' must be a positive int")
    if size > YAML_SIZE_CAP:
        raise ValueError("generate 'size_bytes' exceeds the size cap")
    facts = spec.get("facts", {})
    if not isinstance(facts, dict):
        raise ValueError("generate 'facts' must be a mapping")
    _FACT_VALIDATORS[kind](facts, paths)


def materialize_fixture(
    files: dict[str, str],
    generate: list[dict],
    workspace: Path,
) -> None:
    """Write static ``files:`` (newline="" preserves CRLF) then build and
    write every ``generate:`` spec as LF bytes. Shared by the runner and
    the hermetic tests so both build byte-identical workspaces."""
    for rel, content in files.items():
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
    for spec in generate or []:
        built = _BUILDERS[spec["kind"]](spec)
        for rel, raw in built.items():
            target = workspace / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
