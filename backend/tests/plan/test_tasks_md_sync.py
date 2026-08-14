"""T-14 acceptance 5: docs/TASKS.md agrees with docs/tickets.json
field-by-field.

Per the ticket's own contract ("ideally by re-rendering and diffing, so
drift is impossible"), this re-renders docs/TASKS.md from the CURRENT
docs/tickets.json using the committed scripts/render_tasks_md.py and diffs
the result against the committed file byte-for-byte. Any future hand-edit of
either file -- the exact failure mode that drifted TASKS.md from
tickets.json on T-3, T-7 and T-8 before this ticket -- makes this test fail
immediately, rather than needing a human to notice.

scripts/ has no __init__.py (it is a bag of standalone CLI scripts, not a
Python package -- see scripts/live_smoke.py, scripts/verify_deploy.sh), so
the renderer is loaded by file path via importlib rather than a normal
package import.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[3]
RENDERER_PATH = REPO_ROOT / "scripts" / "render_tasks_md.py"
TICKETS_PATH = REPO_ROOT / "docs" / "tickets.json"
TASKS_MD_PATH = REPO_ROOT / "docs" / "TASKS.md"


def _load_renderer() -> ModuleType:
    spec = importlib.util.spec_from_file_location("render_tasks_md", RENDERER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tasks_md_is_exactly_what_the_renderer_produces_from_tickets_json() -> None:
    renderer = _load_renderer()
    data = json.loads(TICKETS_PATH.read_text())
    expected = renderer.render(data)
    actual = TASKS_MD_PATH.read_text()
    assert actual == expected, (
        "docs/TASKS.md has drifted from docs/tickets.json: re-rendering with "
        "scripts/render_tasks_md.py produces different content than the "
        "committed file. Run 'uv run python scripts/render_tasks_md.py' and "
        "commit the result -- never hand-edit docs/TASKS.md."
    )


def test_every_ticket_id_appears_exactly_once_in_tasks_md() -> None:
    """A cheap, human-legible sanity check independent of the byte-diff
    above: every ticket in tickets.json gets its own '### T-<id>:' heading,
    exactly once, so a ticket can never be silently dropped from the
    mirror."""
    data = json.loads(TICKETS_PATH.read_text())
    text = TASKS_MD_PATH.read_text()
    for ticket in data["tickets"]:
        heading = f"### {ticket['id']}: "
        assert text.count(heading) == 1, (
            f"expected exactly one {heading!r} heading in docs/TASKS.md, "
            f"found {text.count(heading)}"
        )
