"""T-14 acceptance 5: docs/TASKS.md agrees with docs/tickets.json
field-by-field.

MIGRATED under T-31 (harness-sync migration): this test used to load
``scripts/render_tasks_md.py`` (the v1 renderer) via importlib, call its
``render(data)`` function, and diff the result against the committed
docs/TASKS.md byte-for-byte. Commit c44f9af ("cc-factory: harness sync")
replaced the renderer that actually owns docs/TASKS.md: `.claude/scripts/
gen_tasks.py` now regenerates it, invoked automatically by
`harness_lib.py cmd_close` after every successful ticket close (see
`harness_lib.py`'s `cmd_close`, final lines). `scripts/render_tasks_md.py`
is still present in the tree but is dead code as far as docs/TASKS.md is
concerned -- it emits mermaid graphs and a different header, and will never
agree with what gen_tasks.py produces. Asserting drift against it would be
asserting the wrong contract, not preserving the old one, so this test now
targets `.claude/scripts/gen_tasks.py`, the renderer that actually owns the
file.

The intent this test protects is unchanged from T-14: docs/TASKS.md is
GENERATED, never hand-edited, and any drift between it and its inputs
(docs/tickets.json, plus the live `.claude/claims/` / `.claude/evidence/`
state that `gen_tasks.py`'s per-ticket `status()` call reads) is a failure a
human should see immediately rather than a human needing to notice.

`gen_tasks.py` is a script, not a library -- it has no importable
`render(data)` function; run top to bottom it resolves `ROOT` from
`CLAUDE_PROJECT_DIR` (falling back to `git rev-parse --show-toplevel`) and
writes straight to `<ROOT>/docs/TASKS.md`. To diff its output against the
committed file without ever mutating the real repo's docs/TASKS.md (or its
`.claude/claims/` / `.claude/evidence/` state -- T-31's hermeticity rule),
this test builds a synthetic project directory in `tmp_path`: a copy of the
real `.claude/scripts/harness_lib.py` and `gen_tasks.py`, a copy of the real
`docs/tickets.json`, and copies of whatever the real `.claude/claims/` and
`.claude/evidence/` directories currently hold (the same inputs `status()`
would read live), then runs `gen_tasks.py` as a subprocess with
`CLAUDE_PROJECT_DIR` pointed at that copy. Its output is compared against
the REAL, on-disk docs/TASKS.md, read but never written by this test.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
GEN_TASKS_PATH = REPO_ROOT / ".claude" / "scripts" / "gen_tasks.py"
HARNESS_LIB_PATH = REPO_ROOT / ".claude" / "scripts" / "harness_lib.py"
TICKETS_PATH = REPO_ROOT / "docs" / "tickets.json"
TASKS_MD_PATH = REPO_ROOT / "docs" / "TASKS.md"
REAL_CLAIMS_DIR = REPO_ROOT / ".claude" / "claims"
REAL_EVIDENCE_DIR = REPO_ROOT / ".claude" / "evidence"


def _build_shadow_project(tmp_path: Path) -> Path:
    """A disposable CLAUDE_PROJECT_DIR that mirrors the real repo's current
    generation inputs exactly, read-only copies only -- nothing here is ever
    written back to the real repo."""
    (tmp_path / ".claude" / "scripts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HARNESS_LIB_PATH, tmp_path / ".claude" / "scripts" / "harness_lib.py")
    shutil.copyfile(GEN_TASKS_PATH, tmp_path / ".claude" / "scripts" / "gen_tasks.py")
    shutil.copyfile(TICKETS_PATH, tmp_path / "docs" / "tickets.json")

    for name, src in (("claims", REAL_CLAIMS_DIR), ("evidence", REAL_EVIDENCE_DIR)):
        dst = tmp_path / ".claude" / name
        dst.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            for f in src.glob("*.json"):
                shutil.copyfile(f, dst / f.name)
    return tmp_path


def _run_gen_tasks(project_dir: Path) -> str:
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    result = subprocess.run(
        [sys.executable, str(project_dir / ".claude" / "scripts" / "gen_tasks.py")],
        capture_output=True,
        text=True,
        env=env,
        cwd=project_dir,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"gen_tasks.py failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    return (project_dir / "docs" / "TASKS.md").read_text()


def test_tasks_md_is_exactly_what_gen_tasks_produces_from_current_state(
    tmp_path: Path,
) -> None:
    project = _build_shadow_project(tmp_path)
    expected = _run_gen_tasks(project)
    actual = TASKS_MD_PATH.read_text()
    assert actual == expected, (
        "docs/TASKS.md has drifted from what .claude/scripts/gen_tasks.py "
        "produces from the current docs/tickets.json plus live "
        ".claude/claims//.claude/evidence/ state. Run "
        "'python3 .claude/scripts/gen_tasks.py' and commit the result -- "
        "never hand-edit docs/TASKS.md (it also regenerates automatically "
        "on every 'claim.sh close')."
    )


def test_every_ticket_id_appears_exactly_once_in_tasks_md() -> None:
    """A cheap, human-legible sanity check independent of the byte-diff
    above: every ticket in tickets.json gets its own '### T-<id>:' heading,
    exactly once, so a ticket can never be silently dropped from the
    mirror. Unchanged by the T-31 migration -- gen_tasks.py emits the same
    '### {id}: {title}' heading shape the v1 renderer did."""
    data = json.loads(TICKETS_PATH.read_text())
    text = TASKS_MD_PATH.read_text()
    for ticket in data["tickets"]:
        heading = f"### {ticket['id']}: "
        assert text.count(heading) == 1, (
            f"expected exactly one {heading!r} heading in docs/TASKS.md, "
            f"found {text.count(heading)}"
        )
