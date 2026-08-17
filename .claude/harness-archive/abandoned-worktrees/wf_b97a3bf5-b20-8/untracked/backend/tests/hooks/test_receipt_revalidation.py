"""W12: a receipt silently stops attesting the moment a later commit touches
its scope, and nothing in the harness ever notices.

A receipt binds a ticket to (close commit, content fingerprint of its scope).
Both are snapshots. `status(tid)` reports "resolved" for as long as the receipt
FILE exists -- it never re-reads the fingerprint -- so an unticketed commit that
edits an attested file leaves a green board behind a certificate that no longer
describes anything in the tree. That is not hypothetical: aea59c0 ("provider:
swap OpenAI for the Anthropic Messages API") touched
backend/tests/hooks/test_close_unattributed_claim_gap.py with no ticket open,
which is inside the attested scope of T-27 and T-28.

These tests drive the REAL lifecycle in throwaway projects (conftest's
_build_project pattern) and assert on `harness_lib.py revalidate`:

  VALID                  fingerprint still reproduces at HEAD
  DRIFTED-EXPLAINED      a later ticket whose OWN scope covers the path changed
                         it inside its own attested window
  DRIFTED-UNATTRIBUTED   nothing in the plan accounts for the change

and on the two properties that make the command usable by an auditor: it must
exit nonzero on DRIFTED-UNATTRIBUTED, and it must be strictly READ-ONLY -- an
auditor runs it against a live repo where another session holds an open claim,
so it may not run `git add`, touch the index, or move the working tree.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from .conftest import REAL_HOOKS_DIR, REAL_SCRIPTS_DIR

HARNESS_REL = ".claude/scripts/harness_lib.py"

# Four synthetic tickets covering the three verdicts plus a shared-scope pair.
PROBE_TICKETS: dict[str, Any] = {
    "project": "w12probe",
    "tickets": [
        {"id": "T-1", "title": "alpha", "scope": ["src/alpha/**"],
         "depends_on": [], "verify": "true", "acceptance": []},
        {"id": "T-2", "title": "beta", "scope": ["src/shared/**"],
         "depends_on": [], "verify": "true", "acceptance": []},
        {"id": "T-3", "title": "gamma", "scope": ["src/shared/**", "src/gamma/**"],
         "depends_on": [], "verify": "true", "acceptance": []},
        {"id": "T-4", "title": "delta", "scope": ["src/delta/**"],
         "depends_on": [], "verify": "true", "acceptance": []},
    ],
}


# ---------------------------------------------------------------------------
# synthetic project (conftest._build_project pattern, extended with lifecycle)
# ---------------------------------------------------------------------------
def _git(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    r = subprocess.run(["git", *args], cwd=project, capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr}"
    return r


def _write(project: Path, rel: str, text: str) -> None:
    p = project / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _harness(project: Path, *args: str,
             sid: str = "sess-probe") -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    env["CLAUDE_PROJECT_DIR"] = str(project)
    env["CLAUDE_CODE_SESSION_ID"] = sid
    return subprocess.run(
        [sys.executable, str(project / HARNESS_REL), *args],
        cwd=project, capture_output=True, text=True, env=env,
    )


def _lifecycle(project: Path, tid: str, edits: dict[str, str], sid: str) -> None:
    """Claim -> edit -> close, through the real scripts, minting a real receipt."""
    for rel, text in edits.items():
        _write(project, rel, text)
    r = _harness(project, "claim", tid, "probe ordering note", sid=sid)
    assert r.returncode == 0, f"claim {tid}: {r.stdout}{r.stderr}"
    r = _harness(project, "close", sid=sid)
    assert r.returncode == 0, f"close {tid}: {r.stdout}{r.stderr}"


@pytest.fixture
def probe(tmp_path: Path) -> Path:
    """A project carrying four real receipts and all three drift shapes.

    T-1  attested, then an UNTICKETED commit edits src/alpha/mod.py
    T-2  attested, then T-3 (which also owns src/shared/**) edits it in-window
    T-3  attested, nothing after it
    T-4  attested, nothing ever touches src/delta/**
    """
    project = tmp_path / "proj"
    project.mkdir()
    (project / "docs").mkdir()
    _write(project, "docs/tickets.json", json.dumps(PROBE_TICKETS))
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(REAL_SCRIPTS_DIR, project / ".claude" / "scripts", ignore=ignore)
    shutil.copytree(REAL_HOOKS_DIR, project / ".claude" / "hooks", ignore=ignore)
    for name in ("alpha", "shared", "gamma", "delta"):
        _write(project, f"src/{name}/mod.py", f"# {name} seed\n")
    _git(project, "init", "-q", "-b", "main")
    _git(project, "config", "user.name", "w12-probe")
    _git(project, "config", "user.email", "w12@example.invalid")
    _git(project, "config", "commit.gpgsign", "false")
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "synthetic project seed")

    _lifecycle(project, "T-1", {"src/alpha/mod.py": "# T-1 work\n"}, "sess-1")
    _lifecycle(project, "T-2", {"src/shared/mod.py": "# T-2 work\n"}, "sess-2")
    _lifecycle(project, "T-4", {"src/delta/mod.py": "# T-4 work\n"}, "sess-4")
    # T-3 owns src/shared/** too -- this edit is inside its own attested window.
    _lifecycle(project, "T-3", {"src/shared/mod.py": "# T-2 work\n# T-3 owns this too\n",
                                "src/gamma/mod.py": "# T-3 work\n"}, "sess-3")
    # ...and now the aea59c0 shape: a commit with no ticket open at all.
    _write(project, "src/alpha/mod.py", "# T-1 work\n# slipped in with no ticket at all\n")
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "chore: tidy alpha")
    return project


def _revalidate(project: Path) -> tuple[int, dict[str, Any], str]:
    r = _harness(project, "revalidate", "--json")
    assert r.stdout.strip(), f"revalidate produced no stdout (rc={r.returncode}): {r.stderr}"
    return r.returncode, json.loads(r.stdout), r.stderr


# ---------------------------------------------------------------------------
# the defect
# ---------------------------------------------------------------------------
def test_status_alone_cannot_see_a_voided_receipt(probe: Path) -> None:
    """The bug, stated as a property: derived status is blind to fingerprint drift.

    This is NOT a test of revalidate -- it pins the gap revalidate exists to fill,
    so that deleting revalidate cannot make the suite green again by accident.
    """
    board = _harness(probe, "status_board")
    assert board.returncode == 0
    assert ["T-1", "resolved"] == board.stdout.splitlines()[0].split()[:2]
    stored = json.loads((probe / ".claude/evidence/T-1.json").read_text())["fingerprint"]
    # ...while the fingerprint it certifies no longer describes the tree.
    sys.path.insert(0, str(probe / ".claude" / "scripts"))
    for mod in [m for m in list(sys.modules) if m == "harness_lib"]:
        del sys.modules[mod]
    os.environ["CLAUDE_PROJECT_DIR"] = str(probe)
    try:
        import harness_lib  # type: ignore[import-not-found]
        now = harness_lib.fingerprint(["src/alpha/**"])
    finally:
        sys.path.pop(0)
        del sys.modules["harness_lib"]
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
    assert now != stored, "probe did not actually void T-1's receipt"


def test_unknown_verb_does_not_pass_silently(probe: Path) -> None:
    """`harness_lib.py <typo>` used to print nothing and exit 0.

    An auditor who runs a verb this harness does not have must not be told
    everything is fine. This is how "revalidate" itself read as green before
    it existed.
    """
    r = _harness(probe, "no-such-verb")
    assert r.returncode != 0, "an unknown verb exited 0 with no output"
    assert "no-such-verb" in (r.stdout + r.stderr)


def test_the_documented_status_verb_actually_reports_something(probe: Path) -> None:
    """.claude/rules/harness-protocol.md: "Run `claim.sh status`" at session start,
    and names status as one of the four lifecycle verbs. The elif chain only
    answered to "status_board", so that command printed nothing and exited 0 --
    a documented control that has never run. Same defect class as the silent
    unknown verb above, which is why fixing one without the other would just
    convert a silent no-op into a hard failure at every session start.
    """
    r = _harness(probe, "status")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "T-1" in r.stdout and "resolved" in r.stdout, r.stdout
    assert r.stdout == _harness(probe, "status_board").stdout


# ---------------------------------------------------------------------------
# the three verdicts
# ---------------------------------------------------------------------------
def test_untouched_receipt_is_valid(probe: Path) -> None:
    _, report, _ = _revalidate(probe)
    by_id = {r["ticket"]: r for r in report["receipts"]}
    assert by_id["T-4"]["verdict"] == "VALID"
    assert by_id["T-3"]["verdict"] == "VALID"


def test_later_ticket_touching_shared_scope_is_explained(probe: Path) -> None:
    _, report, _ = _revalidate(probe)
    t2 = {r["ticket"]: r for r in report["receipts"]}["T-2"]
    assert t2["verdict"] == "DRIFTED-EXPLAINED", t2
    changed = {c["path"]: c for c in t2["changes"]}
    assert "src/shared/mod.py" in changed
    assert "T-3" in changed["src/shared/mod.py"]["attributed_to"]


def test_unticketed_commit_is_unattributed(probe: Path) -> None:
    rc, report, _ = _revalidate(probe)
    t1 = {r["ticket"]: r for r in report["receipts"]}["T-1"]
    assert t1["verdict"] == "DRIFTED-UNATTRIBUTED", t1
    changed = {c["path"]: c for c in t1["changes"]}
    assert changed["src/alpha/mod.py"]["attributed_to"] == []
    # names the offending commit, the way every other harness refusal names its file
    assert changed["src/alpha/mod.py"]["commits"], "no commit named for the drift"


def test_exits_nonzero_only_on_unattributed_drift(probe: Path) -> None:
    rc, report, _ = _revalidate(probe)
    assert rc == 1, f"expected rc=1 for unattributed drift, got {rc}"
    assert report["counts"]["DRIFTED-UNATTRIBUTED"] == 1
    assert report["counts"]["DRIFTED-EXPLAINED"] == 1
    assert report["counts"]["VALID"] == 2


def test_verdicts_read_the_plan_not_just_the_commit_graph(probe: Path) -> None:
    """Identical history, one scope line changed -> the verdict must flip.

    T-2's drift is EXPLAINED only because T-3's scope names src/shared/**.
    Take that away and the very same commit stops explaining anything. If this
    test can't flip the verdict, the classifier is counting commits rather than
    reading the contract.
    """
    before = {r["ticket"]: r["verdict"] for r in _revalidate(probe)[1]["receipts"]}
    assert before["T-2"] == "DRIFTED-EXPLAINED"

    doc = json.loads((probe / "docs/tickets.json").read_text())
    for t in doc["tickets"]:
        if t["id"] == "T-3":
            t["scope"] = ["src/gamma/**"]
    _write(probe, "docs/tickets.json", json.dumps(doc))
    _git(probe, "add", "-A")
    _git(probe, "commit", "-q", "-m", "plan: narrow T-3")

    rc, report, _ = _revalidate(probe)
    t2 = {r["ticket"]: r for r in report["receipts"]}["T-2"]
    assert t2["verdict"] == "DRIFTED-UNATTRIBUTED", t2
    assert t2["changes"][0]["attributed_to"] == []


def test_a_later_ratified_edit_does_not_absolve_the_earlier_unticketed_one(probe: Path) -> None:
    """Attribution is settled per COMMIT, not per path.

    Writing a new ticket over a file that an unticketed commit already touched
    must not retroactively clear that commit. Aggregating attribution across a
    path would make one ratified touch vouch for everything that ever happened
    to the file -- which is the shape of the laundering this command exists to
    detect. The real remedy is a human decision (re-earn the receipt), not
    another edit; revalidate's job is to keep saying so.
    """
    doc = json.loads((probe / "docs/tickets.json").read_text())
    doc["tickets"].append({"id": "T-5", "title": "epsilon", "scope": ["src/alpha/**"],
                           "depends_on": [], "verify": "true", "acceptance": []})
    _write(probe, "docs/tickets.json", json.dumps(doc))
    _git(probe, "add", "-A")
    _git(probe, "commit", "-q", "-m", "plan: add T-5")
    _lifecycle(probe, "T-5", {"src/alpha/mod.py": "# T-1 work\n# now owned by T-5\n"}, "sess-5")

    rc, report, _ = _revalidate(probe)
    t1 = {r["ticket"]: r for r in report["receipts"]}["T-1"]
    assert t1["verdict"] == "DRIFTED-UNATTRIBUTED", t1
    # cmd_claim sweeps the working tree into the ticket-START commit, so T-5's
    # content lands there rather than in ticket-close -- assert on attribution,
    # not on which of its two boundary commits carried the bytes.
    per_commit = {c["subject"]: c["attributed_to"] for c in t1["changes"][0]["commits"]}
    assert per_commit["chore: tidy alpha"] == [], "the unticketed commit was absolved"
    assert ["T-5"] in per_commit.values(), f"T-5's own edit lost its attribution: {per_commit}"
    assert rc == 1


def test_out_of_scope_change_inside_a_ticket_window_is_not_explained(probe: Path) -> None:
    """A commit sitting inside SOME ticket's window explains nothing on its own.

    This is exactly W15's residue: T-7's close range carried
    docs/eval-report/** although T-7's scope never named it. Membership of a
    window must not launder a path the window's own contract excludes.
    """
    # T-4 is closed and quiet. Open T-3 again? No -- instead make a fresh ticket
    # whose window will contain an edit to src/delta/** (T-4's attested scope,
    # outside the new ticket's own scope). The scope guard is a PreToolUse hook,
    # so a Bash-shaped write reaches the tree exactly like this.
    doc = json.loads((probe / "docs/tickets.json").read_text())
    doc["tickets"].append({"id": "T-6", "title": "zeta", "scope": ["src/zeta/**"],
                           "depends_on": [], "verify": "true", "acceptance": []})
    _write(probe, "docs/tickets.json", json.dumps(doc))
    _write(probe, "src/zeta/mod.py", "# zeta seed\n")
    _git(probe, "add", "-A")
    _git(probe, "commit", "-q", "-m", "plan: add T-6")
    r = _harness(probe, "claim", "T-6", "probe", sid="sess-6")
    assert r.returncode == 0, r.stdout + r.stderr
    _write(probe, "src/zeta/mod.py", "# T-6 work\n")
    _write(probe, "src/delta/mod.py", "# T-4 work\n# T-6 reached outside its scope\n")
    _git(probe, "add", "-A")
    _git(probe, "commit", "-q", "-m", "T-6: work, plus a reach outside scope")
    # close would fail integrity; the point is that the COMMIT is already in the
    # window, so revalidate must not treat window membership as attribution.
    rc, report, _ = _revalidate(probe)
    t4 = {r["ticket"]: r for r in report["receipts"]}["T-4"]
    assert t4["verdict"] == "DRIFTED-UNATTRIBUTED", t4
    changed = {c["path"]: c for c in t4["changes"]}
    assert changed["src/delta/mod.py"]["attributed_to"] == []
    assert "T-6" in changed["src/delta/mod.py"]["note"]


def test_hand_written_boundary_subjects_do_not_launder_a_change(probe: Path) -> None:
    """`ticket-start:` / `ticket-close:` are commit SUBJECTS, not evidence.

    `git commit -m "ticket-close: T-3"` writes one and git does not care. If
    window membership alone granted attribution, any unticketed edit could be
    laundered into a closed ticket's scope by bracketing it with two strings.
    The boundary must be corroborated by the receipt, which lives in
    .claude/evidence/** and is ABSOLUTE — no ticket scope unlocks it.
    """
    # T-3's real receipt is already minted and points at its real close commit.
    # Forge a fresh T-3 window AFTER it, around an edit to T-2's attested scope.
    _git(probe, "commit", "-q", "--allow-empty", "-m", "ticket-start: T-3")
    _write(probe, "src/shared/mod.py", "# laundered through a forged T-3 window\n")
    _git(probe, "add", "-A")
    _git(probe, "commit", "-q", "-m", "work under the forged window")
    _git(probe, "commit", "-q", "--allow-empty", "-m", "ticket-close: T-3")

    rc, report, _ = _revalidate(probe)
    t2 = {r["ticket"]: r for r in report["receipts"]}["T-2"]
    changed = {c["path"]: c for c in t2["changes"]}
    assert changed["src/shared/mod.py"]["attributed_to"] == [], (
        "a hand-written ticket-close subject laundered an edit into T-3's scope")
    assert t2["verdict"] == "DRIFTED-UNATTRIBUTED"
    assert rc != 0


# ---------------------------------------------------------------------------
# the auditor's precondition: strictly read-only
# ---------------------------------------------------------------------------
def _tree_state(project: Path) -> tuple[Any, ...]:
    index = project / ".git" / "index"
    idx = hashlib.sha256(index.read_bytes()).hexdigest() if index.exists() else None
    status = subprocess.run(["git", "status", "--porcelain", "-z", "-uall"],
                            cwd=project, capture_output=True, text=True).stdout
    head = subprocess.run(["git", "rev-parse", "HEAD"],
                          cwd=project, capture_output=True, text=True).stdout
    files = sorted(
        (str(p.relative_to(project)), hashlib.sha256(p.read_bytes()).hexdigest())
        for p in project.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(project).parts
    )
    return (idx, status, head, tuple(files))


def test_revalidate_is_read_only_with_a_dirty_tree_and_an_open_claim(probe: Path) -> None:
    """An auditor runs this against a LIVE repo: another session holds a claim,
    the tree is mid-edit. `git add -A` here would stage that session's work
    (which is exactly why `harness_lib.py integrity` cannot be used for audit).
    """
    # T-3 already has a receipt, so `claim` would refuse -- write the claim
    # record directly, the way conftest.write_claim does, to model an open claim.
    claim = probe / ".claude" / "claims" / "sess-open.json"
    claim.parent.mkdir(parents=True, exist_ok=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=probe,
                          capture_output=True, text=True).stdout.strip()
    claim.write_text(json.dumps({"ticket": "T-3", "session": "sess-open",
                                 "note": "open", "start_commit": head,
                                 "attempts": 0, "ts": 0}))
    _write(probe, "src/gamma/mod.py", "# uncommitted work in flight\n")
    _write(probe, "src/brand_new_untracked.py", "# never seen by git\n")

    before = _tree_state(probe)
    rc, report, _ = _revalidate(probe)
    after = _tree_state(probe)

    assert before[0] == after[0], "revalidate mutated .git/index (it staged something)"
    assert before[1] == after[1], "revalidate changed what git status reports"
    assert before[2] == after[2], "revalidate moved HEAD"
    assert before[3] == after[3], "revalidate wrote to the working tree"
    assert report["receipts"], "revalidate produced no verdicts"


def test_revalidate_verdicts_ignore_uncommitted_work(probe: Path) -> None:
    """Verdicts are a property of committed history, not of whatever the
    auditor's colleague happens to have open in an editor."""
    rc_clean, clean, _ = _revalidate(probe)
    _write(probe, "src/delta/mod.py", "# uncommitted edit to an attested file\n")
    rc_dirty, dirty, _ = _revalidate(probe)
    assert rc_clean == rc_dirty
    assert {r["ticket"]: r["verdict"] for r in clean["receipts"]} == \
           {r["ticket"]: r["verdict"] for r in dirty["receipts"]}


def test_receipt_for_an_unknown_ticket_is_unverifiable_not_valid(probe: Path) -> None:
    """"I cannot check this" and "this is fine" are different answers.

    Same doctrine as IntegrityUnavailable: an unanswerable question must not
    share an exit code with a clean pass.
    """
    (probe / ".claude/evidence/T-99.json").write_text(json.dumps(
        {"ticket": "T-99", "session": "s", "verify": "true",
         "commit": "0" * 40, "fingerprint": "deadbeef", "attempts": 0, "ts": 0}))
    rc, report, _ = _revalidate(probe)
    t99 = {r["ticket"]: r for r in report["receipts"]}["T-99"]
    assert t99["verdict"] == "UNVERIFIABLE"
    assert rc != 0
