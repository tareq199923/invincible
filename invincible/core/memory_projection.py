# invincible/core/memory_projection.py
"""Memory-graph projection (Level 1: derived relationships only).

STRICTLY A PROJECTION, exactly like ``core/projection.py``: every node is
derived from authoritative stores (MemoryStore rows, ProjectService
projects). The projection owns no state of its own and is never a source
of truth. Reads go through the stores' public APIs only; the CALLER owns
authz - it resolves the owning user and passes ``user_id`` in.

Level 1 by design: relationships are DERIVED from columns that already
exist (project_id, provenance, created_at) - no memory-to-memory semantic
edges, no new schema. The payload (nodes/edges/timeline/summary/layout)
is the permanent data contract; the SVG template that renders it is
disposable and expected to be replaced by a future UI redesign, which
will consume this same shape (the JSON sibling of the dashboard page).
"""
import math
import time

# How many individual memory nodes the graph carries before truncating
# (newest-first). Never silently dropped: ``summary.truncated`` and the
# shown/total pair make the cut visible everywhere the payload renders.
MEMORY_NODE_CAP = 100

# Deterministic source colors - the renderer and the legend share these.
_SOURCE_PALETTE = (
    "#0366d6",  # blue
    "#28a745",  # green
    "#d29922",  # amber
    "#bc2c3d",  # red
    "#8250df",  # purple
    "#1f6feb",  # bright blue
    "#2da44e",  # bright green
    "#bf8700",  # dark amber
    "#cf222e",  # bright red
    "#953800",  # brown
)

# Layout geometry for the center-radial map (viewBox units).
_CANVAS_W = 800
_CANVAS_H = 560
_CENTER = (_CANVAS_W / 2, 240)
_PROJECT_RING = 175
_MEMORY_RINGS = (85, 140, 195)  # outward rings around each project
_SOURCE_ROW_Y = 40

_USER_COLOR = "#24292f"
_PROJECT_COLOR = "#57606a"


def classify_source(provenance: str | None) -> str:
    """Collapse a provenance string to a graph source label.

    ``mcp:<client_name>`` keeps its client name (mcp:claude, mcp:grok are
    distinct sources); ``chat:<session>`` collapses to ``chat``; a NULL
    provenance is a dashboard explicit save.
    """
    if not provenance:
        return "dashboard"
    if provenance.startswith("chat:"):
        return "chat"
    return provenance


def source_color(source: str) -> str:
    """Stable palette assignment: the same source always paints the same
    color, within and across requests."""
    return _SOURCE_PALETTE[sum(ord(c) for c in source) % len(_SOURCE_PALETTE)]


def _layout(nodes: list[dict], edges: list[dict]) -> dict:
    """Deterministic center-radial positions for every node.

    Pure geometry - user at the center, projects on an inner ring,
    memories on outward rings around their project, sources on a top row.
    No physics, no randomness: the same payload always lays out
    identically, and a redesigned renderer can reuse or ignore it."""
    positions: dict[str, dict] = {
        n["id"]: {"x": _CENTER[0], "y": _CENTER[1], "color": _USER_COLOR}
        for n in nodes if n["kind"] == "user"
    }
    projects = [n for n in nodes if n["kind"] == "project"]
    for i, node in enumerate(projects):
        angle = 2 * math.pi * i / max(1, len(projects)) - math.pi / 2
        positions[node["id"]] = {
            "x": _CENTER[0] + _PROJECT_RING * math.cos(angle),
            "y": _CENTER[1] + _PROJECT_RING * math.sin(angle),
            "color": _PROJECT_COLOR,
        }
    # Memory nodes cluster around their project (belongs_to edges tell
    # us which); source-only neighbors are irrelevant to placement.
    by_project: dict[str, list[dict]] = {}
    for edge in edges:
        if edge["kind"] == "belongs_to":
            by_project.setdefault(edge["target"], []).append(edge["source"])
    for project_id, memory_ids in by_project.items():
        anchor = positions.get(project_id)
        if anchor is None:
            continue
        for i, mem_id in enumerate(sorted(memory_ids)):
            ring = _MEMORY_RINGS[min(i // 14, len(_MEMORY_RINGS) - 1)]
            angle = 2 * math.pi * (i % 14) / 14 + (i // 14) * 0.35
            positions[mem_id] = {
                "x": anchor["x"] + ring * math.cos(angle),
                "y": anchor["y"] + ring * math.sin(angle),
                "color": None,  # filled per-source by the caller
            }
    for node in nodes:
        pos = positions.get(node["id"])
        if pos is not None and pos["color"] is None:
            pos["color"] = source_color(node["source"])
    sources = [n for n in nodes if n["kind"] == "source"]
    if sources:
        span = _CANVAS_W * 0.8
        for i, node in enumerate(sources):
            positions[node["id"]] = {
                "x": _CANVAS_W / 2 - span / 2 + span * i / (len(sources) - 1)
                if len(sources) > 1 else _CANVAS_W / 2,
                "y": _SOURCE_ROW_Y,
                "color": source_color(node["label"]),
            }
    return positions


async def build_memory_projection(
    memory_store,
    project_service,
    *,
    user_id: int,
    user_label: str = "you",
    kind: str | None = None,
    project_id: int | None = None,
    limit: int = MEMORY_NODE_CAP,
) -> dict:
    """Assemble the memory-graph payload for one owner.

    ``user_id`` scopes every read (the caller resolved authz); ``kind``
    and ``project_id`` narrow the view with the same union semantics as
    every other memory read path. Memory nodes are capped newest-first
    at ``limit`` with the truncation made explicit in the summary; the
    per-source/per-project/per-kind counts cover the SHOWN rows while
    ``total`` is the exact filtered count over the whole store."""
    limit = max(1, min(limit, MEMORY_NODE_CAP))
    rows = await memory_store.list_for_user(
        user_id, kind=kind, project_id=project_id, limit=limit)
    total = await memory_store.count_for_user(
        user_id, kind=kind, project_id=project_id)
    projects = {
        p["id"]: p["name"]
        for p in await project_service.list(user_id)
    }

    nodes: list[dict] = [{"id": "user", "kind": "user",
                          "label": user_label}]
    edges: list[dict] = []

    project_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    kind_counts: dict[str, int] = {}
    layer_counts: dict[str, int] = {}
    oldest = newest = None

    for row in rows:
        mem_id = f"memory:{row['id']}"
        source = classify_source(row.get("provenance"))
        if row.get("project_id") is not None:
            proj_node = f"project:{row['project_id']}"
        else:
            proj_node = "project:user-scope"
        project_counts[proj_node] = project_counts.get(proj_node, 0) + 1
        source_counts[source] = source_counts.get(source, 0) + 1
        kind_counts[row["kind"]] = kind_counts.get(row["kind"], 0) + 1
        layer_counts[row["layer"]] = layer_counts.get(row["layer"], 0) + 1
        ts = row["created_at"]
        oldest = ts if oldest is None else min(oldest, ts)
        newest = ts if newest is None else max(newest, ts)
        nodes.append({
            "id": mem_id,
            "kind": "memory",
            "label": row["content"][:80],
            "source": source,
            "memory_kind": row["kind"],
            "layer": row["layer"],
            "project_id": row.get("project_id"),
            "confidence": row["confidence"],
            "ts": ts,
        })
        edges.append({"source": mem_id, "target": proj_node,
                      "kind": "belongs_to"})
        edges.append({"source": mem_id, "target": f"source:{source}",
                      "kind": "saved_by"})

    for proj_node in sorted(project_counts):
        label = ("user-scope" if proj_node == "project:user-scope"
                 else projects.get(int(proj_node.split(":", 1)[1]),
                                   proj_node.split(":", 1)[1]))
        nodes.append({
            "id": proj_node,
            "kind": "project",
            "label": label,
            "count": project_counts[proj_node],
        })
        edges.append({"source": proj_node, "target": "user",
                      "kind": "owned_by"})

    for source in sorted(source_counts):
        nodes.append({
            "id": f"source:{source}",
            "kind": "source",
            "label": source,
            "count": source_counts[source],
        })

    timeline = [
        n["id"] for n in sorted(
            (n for n in nodes if n["kind"] == "memory"
             and n.get("ts") is not None),
            key=lambda n: (n["ts"], n["id"]),
        )
    ]

    week_ago = time.time() - 7 * 86400
    summary = {
        "total": total,
        "shown": len(rows),
        "truncated": total > len(rows),
        "by_source": dict(sorted(source_counts.items())),
        "by_project": {
            (n["label"]): n["count"]
            for n in nodes if n["kind"] == "project"
        },
        "by_kind": dict(sorted(kind_counts.items())),
        "by_layer": dict(sorted(layer_counts.items())),
        "oldest": oldest,
        "newest": newest,
        "growth_7d": sum(1 for r in rows if r["created_at"] >= week_ago),
    }

    layout = _layout(nodes, edges)

    # Timeline positions: 0..1 across the strip, oldest left.
    span = (newest - oldest) if newest and oldest and newest > oldest else 0
    by_id = {n["id"]: n for n in nodes}
    timeline_positions = [
        {
            "id": n_id,
            "label": by_id[n_id]["label"],
            "source": by_id[n_id]["source"],
            "frac": ((by_id[n_id]["ts"] - oldest) / span) if span else 0.5,
            "color": source_color(by_id[n_id]["source"]),
        }
        for n_id in timeline
    ]

    return {
        "user_id": user_id,
        "known": True,  # the caller resolved a live owner; shape parity
        "generated_at": time.time(),
        "nodes": nodes,
        "edges": edges,
        "timeline": timeline,
        "timeline_positions": timeline_positions,
        "summary": summary,
        "layout": layout,
        "canvas": {"width": _CANVAS_W, "height": _CANVAS_H},
    }
