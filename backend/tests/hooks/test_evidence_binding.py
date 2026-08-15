"""T-29: "Evidence binds to the tree it certifies."

T-29's text targets `verify_gate.sh` writing a bare-epoch
`.claude/evidence/<id>.pass`. That script and that format are gone (deleted
whole by commit `c44f9af`, "cc-factory: harness sync"). In the current (v2)
harness the ONLY thing that ever mints evidence is `harness_lib.py`'s
`cmd_close` (invoked via `.claude/scripts/claim.sh close`), and a receipt is
`.claude/evidence/<id>.json` -- e.g. `{"ticket": ..., "session": ...,
"verify": ..., "commit": <HEAD at close>, "fingerprint": <sha256 of scope
content>, "attempts": ..., "ts": <epoch>}`. T-29's core guarantee -- evidence
binds to the tree it certifies -- is already shipped, and more strongly than
the ticket asked: this file proves that from the hooks suite rather than
reimplementing anything.

Coverage map against T-29's acceptance criteria:

  Acceptance 1 (dirty-tree flag in the receipt): NOT implemented here. That
  field would live in `cmd_close`, in `.claude/scripts/harness_lib.py`, which
  is out of this ticket's scope (`.claude/hooks/**` and
  `backend/tests/hooks/**` only). See `.claude/NEEDS_HUMAN.md` for the
  investigation and finding: the flag would be constant (always "clean") at
  the instant `cmd_close` writes the receipt, because it commits
  (`ticket-close: <id>`) immediately before writing it; `fingerprint`
  already supersedes a boolean by content-binding the tree instead of merely
  flagging that *something* changed; and there IS a real, narrower residual
  gap between the tree `verify` examined and the tree that gets committed
  and fingerprinted; a boolean dirty-tree flag would not have caught it
  either. No substitute mechanism is added in the hooks layer.

  Acceptance 2 (legacy `.pass` format): T-31 already set the migration
  policy, and it is STRONGER than what T-29's text asks for ("honoured for
  already-closed tickets"): a bare-epoch `.claude/evidence-v1/<id>.pass` is
  INERT -- never honoured as evidence for any ticket, closed or not, never
  upgraded, never newly written. `test_v2_never_writes_a_legacy_pass_file`
  and `test_task_gate_hook_ignores_a_legacy_pass_file_and_still_blocks`
  below assert the part that is true and shipped: v2 never writes a `.pass`
  file anywhere, and a legacy `.pass` confers nothing -- specifically
  through the hooks layer (`task_gate.sh`), which is the piece not already
  covered by `backend/tests/plan/test_evidence_migration.py`'s
  `test_v1_pass_file_is_inert` (that test drives `status_board` and
  `receipt()` directly; it never drives the `hook-taskgate` PreToolUse/
  TaskCompleted gate a live session actually hits). This file does not
  reimplement that test's coverage of `status_board`/`claim` amnesty for a
  `.pass`-bearing ticket -- see that module for that.

  Acceptance 3 (hooks tests: commit binds to HEAD, fingerprint is content-
  bound, `ts` is a plausible epoch): `test_receipt_commit_equals_head_at_
  close_time` and `test_receipt_ts_is_a_plausible_epoch` below, plus
  `test_fingerprint_changes_for_scope_content_but_not_for_out_of_scope_
  changes`, which is the one assertion the existing `test_verify_gate.py`
  coverage did not yet make: an out-of-scope file's content changing must
  NOT move the fingerprint, only a scope file's content changing may.

  Acceptance 4 (hook header documents the binding): see the T-29 comment
  block added to `.claude/hooks/task_gate.sh`'s header.
  `test_task_gate_header_documents_the_binding_and_its_limits` below pins
  that documentation so it cannot silently regress.

Every test builds its own disposable project in `tmp_path`: `git init`, a
hand-authored `docs/tickets.json`, and copies of the real
`.claude/scripts/` and `.claude/hooks/` -- never the real repo's own
`.claude/claims/` or `.claude/evidence/` (this session holds a live claim on
T-29 there right now).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_SCRIPTS_DIR = REPO_ROOT / ".claude" / "scripts"
REAL_HOOKS_DIR = REPO_ROOT / ".claude" / "hooks"
TASK_GATE_SH = REAL_HOOKS_DIR / "task_gate.sh"

# Guardrails: this module must never touch the real repo's live lifecycle state.
REAL_CLAIMS_DIR = REPO_ROOT / ".claude" / "claims"
REAL_EVIDENCE_DIR = REPO_ROOT / ".claude" / "evidence"


# ---------------------------------------------------------------------------
# synthetic-project construction (same shape as test_verify_gate.py /
# backend/tests/plan/test_evidence_migration.py, kept self-contained here)
# ---------------------------------------------------------------------------
def _ticket(tid: str, scope: list[str], verify: str = "true") -> dict:
    return {
        "id": tid,
        "title": f"synthetic ticket {tid}",
        "objective": "T-29 evidence-binding test fixture",
        "acceptance": ["n/a"],
        "verify": verify,
        "scope": scope,
        "depends_on": [],
        "non_goals": [],
        "parallel_safe": False,
        "status": "open",
    }


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30)


def _build_project(
    tmp_path: Path, tickets: list[dict], file_contents: dict[str, str]
) -> Path:
    root = tmp_path
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "tickets.json").write_text(json.dumps({"tickets": tickets}, indent=2))

    shutil.copytree(
        REAL_SCRIPTS_DIR, root / ".claude" / "scripts", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copytree(
        REAL_HOOKS_DIR, root / ".claude" / "hooks", ignore=shutil.ignore_patterns("__pycache__")
    )

    for rel, content in file_contents.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    assert _git(["init", "-q"], root).returncode == 0
    _git(["config", "user.email", "t29-test@example.com"], root)
    _git(["config", "user.name", "T-29 Test"], root)
    _git(["config", "commit.gpgsign", "false"], root)
    _git(["add", "-A"], root)
    r = _git(["commit", "-q", "-m", "initial"], root)
    assert r.returncode == 0, r.stderr

    for guard_dir in (REAL_CLAIMS_DIR, REAL_EVIDENCE_DIR):
        assert guard_dir != root, "test must never alias the real repo's lifecycle state"

    return root


def _env(root: Path, session: str | None) -> dict[str, str]:
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(root)
    if session is None:
        env.pop("CLAUDE_CODE_SESSION_ID", None)
    else:
        env["CLAUDE_CODE_SESSION_ID"] = session
    return env


def _run_harness(
    root: Path, args: list[str], *, session: str | None = "sess-t29"
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(root / ".claude" / "scripts" / "harness_lib.py"), *args],
        capture_output=True, text=True, env=_env(root, session), cwd=root, timeout=30,
    )


def _claim(root: Path, tid: str, note: str, session: str) -> subprocess.CompletedProcess[str]:
    return _run_harness(root, ["claim", tid, note], session=session)


def _close(root: Path, session: str) -> subprocess.CompletedProcess[str]:
    return _run_harness(root, ["close"], session=session)


def _fingerprint_cli(root: Path, tid: str) -> str:
    result = _run_harness(root, ["fingerprint", tid], session=None)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _head(root: Path) -> str:
    return _git(["rev-parse", "HEAD"], root).stdout.strip()


def _evidence_file(root: Path, tid: str) -> Path:
    return root / ".claude" / "evidence" / f"{tid}.json"


def _read_json(p: Path) -> dict:
    return json.loads(p.read_text())


def _task_gate(
    root: Path, payload: dict, session: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(root / ".claude" / "hooks" / "task_gate.sh")],
        input=json.dumps(payload), cwd=root, capture_output=True, text=True,
        env=_env(root, session), timeout=30,
    )


def _task_completed(subject: str) -> dict:
    return {"hook_event_name": "TaskCompleted", "task": {"subject": subject}}


# ---------------------------------------------------------------------------
# Acceptance 3: commit binding
# ---------------------------------------------------------------------------
def test_receipt_commit_equals_head_at_close_time(tmp_path: Path) -> None:
    root = _build_project(
        tmp_path, [_ticket("T-100", ["src/**"])], {"src/module.txt": "v1\n"},
    )
    sid = "sess-commit"
    assert _claim(root, "T-100", "ordering note", sid).returncode == 0
    (root / "src" / "module.txt").write_text("v1\nv2\n")
    close = _close(root, sid)
    assert close.returncode == 0, close.stdout + close.stderr

    receipt = _read_json(_evidence_file(root, "T-100"))
    assert receipt["commit"] == _head(root)
    # The close commit is real history, not a placeholder: HEAD's message names it.
    log = _git(["log", "-1", "--pretty=%s"], root).stdout.strip()
    assert log == "ticket-close: T-100"


# ---------------------------------------------------------------------------
# Acceptance 3: fingerprint is content-bound to SCOPE, and only scope
# ---------------------------------------------------------------------------
def test_fingerprint_changes_for_scope_content_but_not_for_out_of_scope_changes(
    tmp_path: Path,
) -> None:
    """The one assertion T-31's existing coverage (test_verify_gate.py,
    test_evidence_migration.py) never made: a ticket's fingerprint is bound
    to its *scope*, not to the whole tree. Changing a file the ticket's
    scope glob does not match must leave the fingerprint untouched --
    otherwise unrelated concurrent work elsewhere in the repo would falsely
    invalidate every open ticket's evidence.
    """
    root = _build_project(
        tmp_path,
        [_ticket("T-100", ["src/**"])],
        {"src/module.txt": "original\n", "outside/unrelated.txt": "untouched\n"},
    )
    baseline = _fingerprint_cli(root, "T-100")

    # Out-of-scope content change: fingerprint must NOT move.
    (root / "outside" / "unrelated.txt").write_text("changed but irrelevant\n")
    after_outside_edit = _fingerprint_cli(root, "T-100")
    assert after_outside_edit == baseline, (
        "fingerprint moved when only an out-of-scope file's content changed"
    )

    # In-scope content change: fingerprint MUST move.
    (root / "src" / "module.txt").write_text("materially different\n")
    after_scope_edit = _fingerprint_cli(root, "T-100")
    assert after_scope_edit != baseline, (
        "fingerprint did not move when a scope file's content changed"
    )


# ---------------------------------------------------------------------------
# Acceptance 3: ts is a plausible epoch
# ---------------------------------------------------------------------------
def test_receipt_ts_is_a_plausible_epoch(tmp_path: Path) -> None:
    root = _build_project(
        tmp_path, [_ticket("T-100", ["src/**"])], {"src/module.txt": "a\n"},
    )
    sid = "sess-ts"
    assert _claim(root, "T-100", "note", sid).returncode == 0

    before = int(time.time())
    close = _close(root, sid)
    after = int(time.time())
    assert close.returncode == 0, close.stdout + close.stderr

    receipt = _read_json(_evidence_file(root, "T-100"))
    ts = receipt["ts"]
    assert isinstance(ts, int)
    # A couple of seconds of slack either side absorbs subprocess/filesystem latency
    # without weakening the check into something that would pass for a stale or
    # fabricated value (e.g. 0, or a v1-style bare timestamp with no other binding).
    assert before - 2 <= ts <= after + 2, f"ts={ts} not within [{before - 2}, {after + 2}]"


# ---------------------------------------------------------------------------
# Acceptance 2: legacy .pass format is inert (hooks-layer coverage)
# ---------------------------------------------------------------------------
def test_v2_never_writes_a_legacy_pass_file(tmp_path: Path) -> None:
    """v2's close path writes exactly one file under .claude/evidence/:
    <id>.json. It never writes, touches, or creates a .pass file or an
    evidence-v1/ directory as a side effect of a normal claim+close cycle.
    """
    root = _build_project(
        tmp_path, [_ticket("T-100", ["src/**"])], {"src/module.txt": "a\n"},
    )
    sid = "sess-no-pass"
    assert _claim(root, "T-100", "note", sid).returncode == 0
    assert _close(root, sid).returncode == 0

    evidence_dir = root / ".claude" / "evidence"
    written = sorted(p.name for p in evidence_dir.iterdir())
    assert written == ["T-100.json"], f"unexpected files written to evidence/: {written}"
    assert not (root / ".claude" / "evidence-v1").exists()


def test_task_gate_hook_ignores_a_legacy_pass_file_and_still_blocks(tmp_path: Path) -> None:
    """Drives the actual hooks-layer consumer (`task_gate.sh` -> hook-taskgate),
    which `backend/tests/plan/test_evidence_migration.py`'s
    `test_v1_pass_file_is_inert` does not: a legacy bare-epoch
    `.claude/evidence-v1/<id>.pass` -- even one whose epoch is deliberately
    fresh, so a naive "any evidence-shaped file exists" check would be
    fooled -- must not unblock TaskCompleted for that ticket. Only a real
    `.claude/evidence/<id>.json` does.

    Deliberate divergence from T-29's acceptance-2 text ("honoured for
    already-closed tickets"): T-31 set the policy of record, which is
    STRONGER -- a .pass file is inert full stop, never honoured, never
    upgraded, for any ticket in any state. This test asserts that stronger,
    shipped behaviour rather than the weaker text in T-29's ticket.
    """
    root = _build_project(
        tmp_path, [_ticket("T-8888", ["src/**"])], {"src/module.txt": "a\n"},
    )
    v1_dir = root / ".claude" / "evidence-v1"
    v1_dir.mkdir(parents=True, exist_ok=True)
    (v1_dir / "T-8888.pass").write_text(str(int(time.time())))

    result = _task_gate(root, _task_completed("T-8888: ship it"))
    assert result.returncode == 2
    assert "T-8888" in result.stderr
    assert not _evidence_file(root, "T-8888").exists()


# ---------------------------------------------------------------------------
# Acceptance 4: the hook header documents the binding and its limits
# ---------------------------------------------------------------------------
def test_task_gate_header_documents_the_binding_and_its_limits() -> None:
    """Pins the T-29 documentation added to task_gate.sh's header so a later
    edit can't silently drop it. Requires: what the receipt proves (the tree
    the verify ran on got committed and content-fingerprinted), what it does
    not prove (that the committed tree is what verify examined at the exact
    instant it ran -- verify runs BEFORE the commit), and the ordering that
    makes the distinction matter (integrity check, then verify, then commit,
    then the receipt is written).
    """
    text = TASK_GATE_SH.read_text()
    lower = text.lower()

    assert "t-29" in lower, "header should attribute the binding doc to T-29"
    assert "certifies the tree the verify ran on" in lower
    assert "not that that tree was committed" in lower or "not to a live snapshot" in lower

    # The ordering that makes the distinction real: integrity -> verify -> commit -> receipt.
    idx_integrity = lower.find("integrity check")
    idx_verify = lower.find("run the ticket's")
    idx_commit = lower.find("git add -a")
    idx_receipt = lower.find("write it")
    assert -1 not in (idx_integrity, idx_verify, idx_commit, idx_receipt), (
        "header must spell out cmd_close's integrity -> verify -> commit -> receipt order"
    )
    assert idx_integrity < idx_verify < idx_commit < idx_receipt


# ---------------------------------------------------------------------------
# Hermeticity guard: nothing in this module ever touches the real repo's own
# live claim/evidence state (this session holds a live claim on T-29 there).
# ---------------------------------------------------------------------------
SYNTHETIC_TICKET_IDS = ("T-100", "T-8888")


def test_never_touched_the_real_repos_live_harness_state() -> None:
    for tid in SYNTHETIC_TICKET_IDS:
        assert not (REAL_EVIDENCE_DIR / f"{tid}.json").exists()
    if REAL_CLAIMS_DIR.is_dir():
        for p in REAL_CLAIMS_DIR.glob("*.json"):
            try:
                doc = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            assert doc.get("ticket") not in SYNTHETIC_TICKET_IDS
