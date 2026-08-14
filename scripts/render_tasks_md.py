#!/usr/bin/env python3
"""Render docs/TASKS.md from docs/tickets.json.

T-14 acceptance 5: docs/TASKS.md is a GENERATED mirror of docs/tickets.json,
not a hand-maintained document. Hand-editing TASKS.md is exactly what let it
drift from tickets.json on T-3, T-7 and T-8 -- this script is the fix.

Usage:
    uv run python scripts/render_tasks_md.py

writes docs/TASKS.md in place from the current docs/tickets.json.

``render(data)`` is the pure function backend/tests/plan/test_tasks_md_sync.py
imports (by file path, via importlib -- this directory is not a package) to
re-render from the CURRENT docs/tickets.json and diff the result against the
committed docs/TASKS.md byte-for-byte, so the mirror cannot silently drift
again.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
TICKETS_PATH = REPO_ROOT / "docs" / "tickets.json"
TASKS_MD_PATH = REPO_ROOT / "docs" / "TASKS.md"

HEADER = """\
# {project} — Task Graph

Human-readable mirror. `docs/tickets.json` is authoritative — the hooks and
native-Tasks ingestion read it. **This file is GENERATED from
docs/tickets.json by `scripts/render_tasks_md.py` (T-14) — do not hand-edit
it. Run `uv run python scripts/render_tasks_md.py` after any change to
docs/tickets.json and commit the result.**

**Merge order for parallel worktrees**: ascending ticket ID, always
(e.g. T-1 merges before T-2; T-8 before T-9 before T-10).

**Pick order**: tickets carrying `"priority": "next"` are claimed BEFORE the
ascending-ID default. See the "Priority batch" section below for that set.
Merge order within the batch is still ascending ID.
"""

PRIORITY_BATCH_INTRO = """\
## Priority batch — remediation

Raised from defects observed during the T-0–T-11 build, verified against the
repository, revised, and re-verified. These carry `"priority": "next"` in
`docs/tickets.json` and are claimed before the ascending-ID default.
GENERATED FROM tickets.json — do not hand-edit; T-14 made this rendering a
committed script (`scripts/render_tasks_md.py`).
"""


def _mermaid_id(ticket_id: str) -> str:
    """"T-12" -> "T12" -- mermaid node ids can't contain "-"."""
    return "T" + ticket_id.split("-", 1)[1]


def _mermaid_node(ticket: dict[str, Any]) -> str:
    return f'{_mermaid_id(ticket["id"])}[{ticket["id"]} {ticket["title"]}]'


def render_dependency_graph(tickets: list[dict[str, Any]]) -> str:
    """A mermaid graph TD derived entirely from each ticket's `depends_on`
    and `priority` fields: tickets carrying `"priority": "next"` are grouped
    into a "remediation" subgraph (edges with both ends inside the batch
    render inside it; a batch ticket with no in-batch edge at all is
    declared standalone inside the subgraph); every other edge, including
    ones that cross from the main sequence into the batch or vice versa,
    renders at the top level, in ticket-JSON order."""
    by_id = {t["id"]: t for t in tickets}
    is_batch = {t["id"]: t.get("priority") == "next" for t in tickets}

    main_edges: list[tuple[str, str]] = []
    batch_edges: list[tuple[str, str]] = []
    cross_edges: list[tuple[str, str]] = []
    batch_ids_with_internal_edge: set[str] = set()

    for t in tickets:
        for dep in t["depends_on"]:
            edge = (dep, t["id"])
            if is_batch[dep] and is_batch[t["id"]]:
                batch_edges.append(edge)
                batch_ids_with_internal_edge.update(edge)
            elif is_batch[dep] or is_batch[t["id"]]:
                cross_edges.append(edge)
            else:
                main_edges.append(edge)

    lines = ["```mermaid", "graph TD"]
    for dep, tid in main_edges:
        lines.append(f"    {_mermaid_node(by_id[dep])} --> {_mermaid_node(by_id[tid])}")
    lines.append("")
    lines.append('    subgraph remediation["Priority batch — claimed first"]')
    for dep, tid in batch_edges:
        lines.append(f"        {_mermaid_node(by_id[dep])} --> {_mermaid_node(by_id[tid])}")
    for t in tickets:
        if is_batch[t["id"]] and t["id"] not in batch_ids_with_internal_edge:
            lines.append(f"        {_mermaid_node(t)}")
    lines.append("    end")
    for dep, tid in cross_edges:
        lines.append(f"    {_mermaid_node(by_id[dep])} --> {_mermaid_node(by_id[tid])}")
    lines.append("```")
    lines.append("")
    lines.append(
        "Parallel waves are computed at execution time; disjoint-scope tickets are\n"
        "marked `parallel_safe`."
    )
    return "\n".join(lines)


def render_ticket(t: dict[str, Any]) -> str:
    lines = [f"### {t['id']}: {t['title']}"]
    lines.append(f"- **Objective**: {t['objective']}")
    lines.append(f"- **Refs**: {', '.join(t['refs']) if t['refs'] else 'none'}")
    lines.append("- **Acceptance**:")
    for i, item in enumerate(t["acceptance"], start=1):
        lines.append(f"  {i}. {item}")
    lines.append(f"- **Verify**: `{t['verify']}`")
    lines.append(f"- **Scope**: {', '.join(f'`{s}`' for s in t['scope'])}")
    lines.append(f"- **Depends on**: {', '.join(t['depends_on']) if t['depends_on'] else 'none'}")
    lines.append("- **Non-goals**:")
    for item in t["non_goals"]:
        lines.append(f"  - {item}")
    lines.append(f"- **Parallel safe**: {'yes' if t.get('parallel_safe') else 'no'}")
    lines.append(f"- **Priority**: {t.get('priority', 'default')}")
    lines.append(f"- **Status**: {t.get('status', 'open')}")
    return "\n".join(lines)


def render(data: dict[str, Any]) -> str:
    tickets: list[dict[str, Any]] = data["tickets"]
    main_tickets = [t for t in tickets if t.get("priority") != "next"]
    batch_tickets = [t for t in tickets if t.get("priority") == "next"]

    parts = [HEADER.format(project=data.get("project", "Project"))]
    parts.append("## Dependency graph\n")
    parts.append(render_dependency_graph(tickets))
    parts.append("\n## Tickets\n")
    parts.append("\n\n".join(render_ticket(t) for t in main_tickets))
    if batch_tickets:
        parts.append("\n" + PRIORITY_BATCH_INTRO)
        parts.append("\n\n".join(render_ticket(t) for t in batch_tickets))

    text = "\n".join(parts)
    if not text.endswith("\n"):
        text += "\n"
    return text


def main() -> None:
    data = json.loads(TICKETS_PATH.read_text())
    TASKS_MD_PATH.write_text(render(data))


if __name__ == "__main__":
    main()
