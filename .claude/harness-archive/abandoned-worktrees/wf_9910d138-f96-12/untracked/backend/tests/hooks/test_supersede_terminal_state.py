"""The SECOND terminal state: `claim.sh supersede "<reason>"`.

Before this existed the harness had exactly two outcomes -- a receipt
(`.claude/evidence/<id>.json`, meaning "the verify RAN and PASSED") or nothing at
all. An agent that had correctly PROVEN a ticket's acceptance unreachable inside
its declared scope therefore had no honest way to end it, and the standing
incentive was to mint a full receipt anyway. That is not hypothetical: T-28 closed
with a receipt while the test file it shipped states in capitals that "THESE TESTS
PASSING DOES NOT MEAN ACCEPTANCE 1 IS MET"
(`backend/tests/hooks/test_close_unattributed_claim_gap.py`, module docstring).

A superseded record is the outcome that was missing. The whole point of these
tests is that it is NOT a soft receipt, in either direction:

  * it must never be MISTAKABLE for a pass -- distinct `kind`, distinct derived
    status, distinct board rendering, none of a receipt's key names, and
    `harness_lib.receipt()` returning None for it so that every existing consumer
    that asks "was this verified?" keeps getting NO without being touched;
  * it must never be an EASIER door than a close -- same claim ownership checks,
    same integrity check, same cross-ticket regression gate, plus a mandatory
    reason, plus a mandatory .claude/NEEDS_HUMAN.md entry, plus the same absolute
    guard refusal on `.claude/evidence/**` that stops an agent forging one by hand.

Self-contained, like `test_verify_gate.py` and
`test_close_unattributed_claim_gap.py`: every test builds its own synthetic git
project under `tmp_path` (real `git init`, a hand-authored `docs/tickets.json`,
copies of the real `.claude/scripts/` and `.claude/hooks/`) and drives the REAL
`claim.sh`/`harness_lib.py` as subprocesses with `CLAUDE_PROJECT_DIR` pointed at
that copy. Nothing here ever touches the real repo's `.claude/claims/`,
`.claude/evidence/`, `docs/`, or git history -- the autouse fixture below is the
trip-wire that proves it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_SCRIPTS_DIR = REPO_ROOT / ".claude" / "scripts"
REAL_HOOKS_DIR = REPO_ROOT / ".claude" / "hooks"

# Far outside the real plan's range (T-0..T-31), so a leak could never be mistaken
# for legitimate state and could never collide with a real ticket id.
SUP = "T-9200"
DEP = "T-9201"
OTHER = "T-9202"
ALL_SYNTHETIC = (SUP, DEP, OTHER)

REASON = "acceptance 1 names verify_gate, which commit c44f9af deleted; no hook can fire here"


@pytest.fixture(autouse=True)
def _never_touch_the_real_repo() -> Iterator[None]:
    """Other sessions may claim/close real tickets while this suite runs, so this
    cannot snapshot-and-diff the whole directory. It asserts instead that this
    file's synthetic ticket ids never appear as a real claim or a real terminal
    record -- the one leak signature that would prove a subprocess escaped its
    tmp_path project."""

    def _check() -> None:
        for tid in ALL_SYNTHETIC:
            assert not (REPO_ROOT / ".claude" / "evidence" / f"{tid}.json").exists()
        claims_dir = REPO_ROOT / ".claude" / "claims"
        if claims_dir.is_dir():
            for p in claims_dir.glob("*.json"):
                try:
                    doc = json.loads(p.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                assert doc.get("ticket") not in ALL_SYNTHETIC

    _check()
    yield
    _check()


# ---------------------------------------------------------------------------
# Synthetic project
# ---------------------------------------------------------------------------
def _git(proj: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=proj, capture_output=True, text=True, check=True
    )


def _ticket(
    tid: str,
    *,
    verify: str = "true",
    scope: list[str] | None = None,
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": tid,
        "title": f"synthetic ticket {tid}",
        "objective": "exercise the supersede terminal state",
        "acceptance": ["synthetic acceptance criterion"],
        "verify": verify,
        "scope": scope if scope is not None else [f"src/{tid}/**"],
        "depends_on": depends_on or [],
        "non_goals": [],
        "parallel_safe": False,
        "status": "open",
    }


def _make_project(
    tmp_path: Path,
    tickets: list[dict[str, Any]],
    *,
    files: dict[str, str] | None = None,
    full_verify: str | None = None,
) -> Path:
    proj = tmp_path / "proj"
    (proj / "docs").mkdir(parents=True)
    (proj / ".claude").mkdir(parents=True)
    shutil.copytree(
        REAL_SCRIPTS_DIR, proj / ".claude" / "scripts",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copytree(
        REAL_HOOKS_DIR, proj / ".claude" / "hooks",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    doc: dict[str, Any] = {"project": "supersede-test", "tickets": tickets}
    if full_verify is not None:
        doc["full_verify"] = full_verify
    (proj / "docs" / "tickets.json").write_text(json.dumps(doc))
    (proj / ".claude" / "NEEDS_HUMAN.md").write_text("# Needs human\n")
    for t in tickets:
        # One real, git-tracked file per ticket scope so scope_files()/fingerprint()
        # have something to hash.
        (proj / "src" / t["id"]).mkdir(parents=True, exist_ok=True)
        (proj / "src" / t["id"] / "work.txt").write_text("initial\n")
    for rel, content in (files or {}).items():
        p = proj / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    # .gitignore keeps interpreter byte-cache out of `git status --porcelain`, which
    # cmd_claim refuses to claim over. Real repos have one; synthetic ones must too.
    (proj / ".gitignore").write_text("__pycache__/\n*.pyc\n")

    _git(proj, "init", "-q")
    _git(proj, "config", "user.email", "supersede-test@example.invalid")
    _git(proj, "config", "user.name", "supersede-test")
    _git(proj, "config", "commit.gpgsign", "false")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-q", "-m", "initial")
    return proj


def _env(proj: Path, session_id: str | None) -> dict[str, str]:
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(proj)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if session_id is None:
        env.pop("CLAUDE_CODE_SESSION_ID", None)
    else:
        env["CLAUDE_CODE_SESSION_ID"] = session_id
    return env


def _claim_sh(
    proj: Path, args: list[str], session_id: str | None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(proj / ".claude" / "scripts" / "claim.sh"), *args],
        cwd=proj, capture_output=True, text=True, env=_env(proj, session_id), timeout=60,
    )


def _claim(
    proj: Path, tid: str, sid: str, note: str = "ordering note"
) -> subprocess.CompletedProcess[str]:
    return _claim_sh(proj, ["claim", tid, note], sid)


def _close(proj: Path, sid: str) -> subprocess.CompletedProcess[str]:
    return _claim_sh(proj, ["close"], sid)


def _supersede(proj: Path, sid: str, reason: str = REASON) -> subprocess.CompletedProcess[str]:
    return _claim_sh(proj, ["supersede", reason], sid)


def _status(proj: Path, tid: str) -> str:
    r = _claim_sh(proj, ["status_board"], None)
    assert r.returncode == 0, r.stderr
    for line in r.stdout.splitlines():
        parts = line.split()
        if parts and parts[0] == tid:
            return parts[1]
    raise AssertionError(f"{tid} missing from status_board: {r.stdout!r}")


def _record(proj: Path, tid: str) -> dict[str, Any]:
    return json.loads((proj / ".claude" / "evidence" / f"{tid}.json").read_text())


def _evidence_path(proj: Path, tid: str) -> Path:
    return proj / ".claude" / "evidence" / f"{tid}.json"


def _claim_path(proj: Path, sid: str) -> Path:
    return proj / ".claude" / "claims" / f"{sid}.json"


def _head(proj: Path) -> str:
    return _git(proj, "rev-parse", "HEAD").stdout.strip()


def _call_harness_fn(proj: Path, expr: str) -> Any:
    """Evaluate an expression against the project's OWN harness_lib copy, so a
    test can assert on the library functions consumers actually call, not just on
    CLI text."""
    code = (
        "import sys, json; "
        f"sys.path.insert(0, {str(proj / '.claude' / 'scripts')!r}); "
        "import harness_lib; "
        f"print(json.dumps({expr}))"
    )
    r = subprocess.run(
        ["python3", "-c", code], cwd=proj, capture_output=True, text=True,
        env=_env(proj, None), timeout=60,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip())


def _supersede_one(tmp_path: Path, **kw: Any) -> tuple[Path, str]:
    """The common arrangement: one ticket, claimed and then superseded."""
    proj = _make_project(tmp_path, [_ticket(SUP, **kw)])
    sid = "sess-sup"
    assert _claim(proj, SUP, sid).returncode == 0
    r = _supersede(proj, sid)
    assert r.returncode == 0, r.stdout + r.stderr
    return proj, sid


# ---------------------------------------------------------------------------
# The record itself: a supersede is not a receipt
# ---------------------------------------------------------------------------
def test_superseded_record_is_kind_discriminated_and_carries_reason_and_commit(
    tmp_path: Path,
) -> None:
    proj, sid = _supersede_one(tmp_path)
    rec = _record(proj, SUP)

    assert rec["kind"] == "superseded"
    assert rec["verified"] is False
    assert rec["reason"] == REASON              # the whole evidentiary content, verbatim
    assert rec["commit"] == _head(proj)         # bound to the tree it was decided on
    assert rec["session"] == sid
    assert rec["ticket"] == SUP


def test_superseded_record_carries_none_of_a_receipts_key_names(tmp_path: Path) -> None:
    """Structural, not cosmetic: a consumer that reads a receipt's keys must fail
    LOUDLY on a superseded record rather than silently reading it as a pass. The
    verify string and the scope hash are still recorded -- under names no receipt
    reader consults -- so a human can see exactly what was not run."""
    proj, _sid = _supersede_one(tmp_path)
    rec = _record(proj, SUP)

    assert "verify" not in rec, "a superseded record must not carry a receipt's 'verify' key"
    assert "fingerprint" not in rec, (
        "a superseded record must not carry a receipt's 'fingerprint' key"
    )
    assert rec["verify_not_run"] == "true"
    assert isinstance(rec["scope_fingerprint"], str) and len(rec["scope_fingerprint"]) == 64


def test_a_receipts_shape_is_unchanged_by_the_arrival_of_the_second_kind(
    tmp_path: Path,
) -> None:
    """The happy path must not move. Stamping `"kind": "receipt"` onto new
    receipts was tried and reverted: absence-means-receipt has to hold anyway for
    the records minted before the discriminator existed, so the stamp buys no
    meaning while breaking test_claim_format.py's exact-key-set pin on a receipt.
    This asserts the same key set from the other side, plus that the harness reads
    an unstamped receipt as a receipt and never as a supersede."""
    proj = _make_project(tmp_path, [_ticket(SUP)])
    sid = "sess-close"
    assert _claim(proj, SUP, sid).returncode == 0
    assert _close(proj, sid).returncode == 0

    rec = _record(proj, SUP)
    assert rec.keys() == {"ticket", "session", "verify", "commit", "fingerprint", "attempts", "ts"}
    assert "reason" not in rec
    assert _status(proj, SUP) == "resolved"
    assert _call_harness_fn(proj, f"harness_lib.superseded({SUP!r})") is None


def test_legacy_receipt_without_a_kind_key_is_still_honoured_as_a_receipt(
    tmp_path: Path,
) -> None:
    """Migration: all 26 receipts already on disk in the real repo predate the
    discriminator and carry no `kind` at all. Absence must keep meaning receipt --
    that is exactly what they were -- or introducing the second kind would silently
    re-queue every finished ticket."""
    proj = _make_project(tmp_path, [_ticket(SUP)])
    (proj / ".claude" / "evidence").mkdir(parents=True, exist_ok=True)
    _evidence_path(proj, SUP).write_text(json.dumps({
        "ticket": SUP, "session": "old", "verify": "true", "commit": _head(proj),
        "fingerprint": "0" * 64, "attempts": 0, "ts": 0,
    }))
    assert _status(proj, SUP) == "resolved"
    assert _call_harness_fn(proj, f"harness_lib.receipt({SUP!r}) is not None") is True
    assert _call_harness_fn(proj, f"harness_lib.superseded({SUP!r})") is None


# ---------------------------------------------------------------------------
# Derived status and the receipt() contract
# ---------------------------------------------------------------------------
def test_status_is_superseded_and_never_resolved(tmp_path: Path) -> None:
    proj, _sid = _supersede_one(tmp_path)
    assert _status(proj, SUP) == "superseded"


def test_receipt_function_returns_none_for_a_superseded_record(tmp_path: Path) -> None:
    """The single most important non-weakening property. Every existing consumer
    asks `receipt(tid)` to mean "was this verified?"; a superseded record must
    answer None to all of them without any of them being updated."""
    proj, _sid = _supersede_one(tmp_path)
    assert _call_harness_fn(proj, f"harness_lib.receipt({SUP!r})") is None
    assert _call_harness_fn(proj, f"harness_lib.superseded({SUP!r})['kind']") == "superseded"
    assert _call_harness_fn(proj, f"harness_lib.evidence({SUP!r})['reason']") == REASON


def test_an_unrecognised_kind_is_honoured_as_neither_pass_nor_supersede(
    tmp_path: Path,
) -> None:
    """Fail-safe on the discriminator itself: only the ABSENCE of `kind` means
    receipt. A record carrying some other kind is not coerced into one -- it
    derives its own status, `receipt()` refuses it, and the ticket cannot be
    claimed over it either."""
    proj = _make_project(tmp_path, [_ticket(SUP)])
    (proj / ".claude" / "evidence").mkdir(parents=True, exist_ok=True)
    _evidence_path(proj, SUP).write_text(json.dumps({"ticket": SUP, "kind": "totally-fine"}))

    assert _status(proj, SUP) == "invalid"
    assert _call_harness_fn(proj, f"harness_lib.receipt({SUP!r})") is None
    assert _call_harness_fn(proj, f"harness_lib.superseded({SUP!r})") is None
    r = _claim(proj, SUP, "sess-x")
    assert r.returncode == 1
    assert "does not recognise" in r.stdout


def test_an_unreadable_record_is_not_reported_as_absent(tmp_path: Path) -> None:
    """A corrupt terminal record must not silently re-queue a finished ticket
    (which would invite a second close over the top of it) nor be read as a pass."""
    proj = _make_project(tmp_path, [_ticket(SUP)])
    (proj / ".claude" / "evidence").mkdir(parents=True, exist_ok=True)
    _evidence_path(proj, SUP).write_text("{not json at all")

    assert _status(proj, SUP) == "invalid"
    assert _call_harness_fn(proj, f"harness_lib.receipt({SUP!r})") is None
    assert _claim(proj, SUP, "sess-x").returncode == 1


# ---------------------------------------------------------------------------
# supersede is not an easier door than close
# ---------------------------------------------------------------------------
def test_supersede_refuses_without_a_claim_held_by_this_session(tmp_path: Path) -> None:
    proj = _make_project(tmp_path, [_ticket(SUP)])
    r = _supersede(proj, "sess-nobody")
    assert r.returncode == 1
    assert "no claim held by this session" in r.stdout
    assert not _evidence_path(proj, SUP).exists()


def test_supersede_acts_only_on_the_calling_sessions_own_claim(tmp_path: Path) -> None:
    """Rule 8: sessions are independent. A session may not supersede a ticket
    another session holds -- that would be a way to terminate someone else's work
    with a record they never wrote."""
    proj = _make_project(tmp_path, [_ticket(SUP)])
    assert _claim(proj, SUP, "sess-owner").returncode == 0
    r = _supersede(proj, "sess-intruder")
    assert r.returncode == 1
    assert "no claim held by this session" in r.stdout
    assert not _evidence_path(proj, SUP).exists()
    assert _claim_path(proj, "sess-owner").exists()  # the owner's claim is untouched


@pytest.mark.parametrize("reason", ["", "   ", "n/a", "too hard", "unreachable"])
def test_supersede_refuses_a_missing_or_thin_reason(tmp_path: Path, reason: str) -> None:
    """No gate is run, so the reason IS the evidence. An empty or one-word reason
    would make the record indistinguishable from giving up."""
    proj = _make_project(tmp_path, [_ticket(SUP)])
    sid = "sess-sup"
    assert _claim(proj, SUP, sid).returncode == 0
    r = _supersede(proj, sid, reason)
    assert r.returncode == 1
    assert "reason" in r.stdout
    assert not _evidence_path(proj, SUP).exists()
    assert _claim_path(proj, sid).exists(), "a refused supersede must leave the claim open"


def test_supersede_still_enforces_the_integrity_check(tmp_path: Path) -> None:
    """Superseding does not amnesty out-of-scope edits. If it did, `supersede`
    would be the laundering route that `close` refuses to be."""
    proj = _make_project(tmp_path, [_ticket(SUP)], files={"unrelated/other.txt": "before\n"})
    sid = "sess-sup"
    assert _claim(proj, SUP, sid).returncode == 0
    (proj / "unrelated" / "other.txt").write_text("touched out of scope\n")

    r = _supersede(proj, sid)
    assert r.returncode == 2
    assert "INTEGRITY FAIL" in r.stdout
    assert "unrelated/other.txt" in r.stdout
    assert not _evidence_path(proj, SUP).exists()
    assert _claim_path(proj, sid).exists()


def test_supersede_refuses_on_a_red_regression_gate(tmp_path: Path) -> None:
    """"my acceptance is unreachable" and "I broke the suite" are different
    claims. The cross-ticket gate still has to be green."""
    proj = _make_project(tmp_path, [_ticket(SUP)], full_verify="false")
    sid = "sess-sup"
    assert _claim(proj, SUP, sid).returncode == 0
    r = _supersede(proj, sid)
    assert r.returncode == 2
    assert "regression gate is RED" in r.stdout
    assert not _evidence_path(proj, SUP).exists()
    assert _claim_path(proj, sid).exists()


def test_supersede_never_runs_the_tickets_own_verify(tmp_path: Path) -> None:
    """Proven with a sentinel the verify command would create if it ever ran. The
    record must also name the gate that was skipped, so a human can run it."""
    sentinel = tmp_path / "verify-actually-ran"
    proj = _make_project(tmp_path, [_ticket(SUP, verify=f"touch {sentinel} && true")])
    sid = "sess-sup"
    assert _claim(proj, SUP, sid).returncode == 0
    assert _supersede(proj, sid).returncode == 0

    assert not sentinel.exists(), "supersede must not run the ticket's verify"
    assert _record(proj, SUP)["verify_not_run"] == f"touch {sentinel} && true"


def test_supersede_refuses_to_overwrite_an_existing_terminal_record(
    tmp_path: Path,
) -> None:
    """Replacing an attestation is a human action. In particular a receipt must
    never be downgradable to a supersede by a later session."""
    proj = _make_project(tmp_path, [_ticket(SUP)])
    assert _claim(proj, SUP, "sess-a").returncode == 0
    assert _close(proj, "sess-a").returncode == 0
    minted = _record(proj, SUP)
    assert _status(proj, SUP) == "resolved"

    # A claim naming an already-receipted ticket cannot be obtained through
    # cmd_claim, so plant one directly -- the harness must still refuse.
    _claim_path(proj, "sess-b").parent.mkdir(parents=True, exist_ok=True)
    _claim_path(proj, "sess-b").write_text(json.dumps({
        "ticket": SUP, "session": "sess-b", "note": "planted",
        "start_commit": _head(proj), "attempts": 0, "ts": 0,
    }))
    r = _supersede(proj, "sess-b")
    assert r.returncode == 1
    assert "already has a terminal record" in r.stdout
    assert _record(proj, SUP) == minted, "the receipt must be byte-identical afterwards"


def test_supersede_announces_itself_in_needs_human(tmp_path: Path) -> None:
    """A supersede is a decision the human has to rule on, so it goes where rule 7
    says such things go -- not buried in a commit body."""
    proj, sid = _supersede_one(tmp_path)
    text = (proj / ".claude" / "NEEDS_HUMAN.md").read_text()
    assert SUP in text
    assert "SUPERSEDED" in text
    assert REASON in text
    assert "HUMAN RULING NEEDED" in text


def test_supersede_makes_its_own_commit_and_clears_the_claim(tmp_path: Path) -> None:
    proj, sid = _supersede_one(tmp_path)
    assert not _claim_path(proj, sid).exists()
    subjects = _git(proj, "log", "--format=%s").stdout.splitlines()
    assert subjects[0] == f"ticket-supersede: {SUP}"
    assert f"ticket-close: {SUP}" not in subjects, "a supersede must not look like a close"


def test_supersede_output_never_claims_anything_was_verified(tmp_path: Path) -> None:
    proj = _make_project(tmp_path, [_ticket(SUP)])
    sid = "sess-sup"
    assert _claim(proj, SUP, sid).returncode == 0
    r = _supersede(proj, sid)
    assert r.returncode == 0
    assert "SUPERSEDED" in r.stdout
    assert "NOT verified" in r.stdout
    assert "No receipt was minted" in r.stdout


# ---------------------------------------------------------------------------
# Re-claiming and dependency propagation
# ---------------------------------------------------------------------------
def test_a_superseded_ticket_cannot_be_reclaimed_and_the_refusal_names_the_reason(
    tmp_path: Path,
) -> None:
    proj, _sid = _supersede_one(tmp_path)
    r = _claim(proj, SUP, "sess-next")
    assert r.returncode == 1
    assert "SUPERSEDED" in r.stdout
    assert REASON in r.stdout
    assert "HUMAN ruling" in r.stdout


def test_a_superseded_dependency_unblocks_dependents_but_warns_loudly(
    tmp_path: Path,
) -> None:
    """The graph must not deadlock behind an unachievable ticket -- that is the
    pressure that produces fake receipts in the first place. But the claim message
    has to say that the edge it just crossed was never verified."""
    proj = _make_project(tmp_path, [_ticket(SUP), _ticket(DEP, depends_on=[SUP])])

    blocked = _claim(proj, DEP, "sess-dep")
    assert blocked.returncode == 1
    assert f"dependency {SUP} has no receipt" in blocked.stdout

    assert _claim(proj, SUP, "sess-sup").returncode == 0
    assert _supersede(proj, "sess-sup").returncode == 0

    unblocked = _claim(proj, DEP, "sess-dep")
    assert unblocked.returncode == 0, unblocked.stdout + unblocked.stderr
    assert "WARNING" in unblocked.stdout
    assert "SUPERSEDED record, not a receipt" in unblocked.stdout
    assert SUP in unblocked.stdout.split("WARNING")[1]
    assert _status(proj, DEP) == "in_progress"


def test_a_missing_dependency_is_still_blocked_after_the_change(tmp_path: Path) -> None:
    """Guard against the obvious over-reach: only a real terminal record satisfies
    an edge. A queued dependency still blocks, with the original message."""
    proj = _make_project(tmp_path, [_ticket(SUP), _ticket(DEP, depends_on=[SUP])])
    r = _claim(proj, DEP, "sess-dep")
    assert r.returncode == 1
    assert f"dependency {SUP} has no receipt" in r.stdout


# ---------------------------------------------------------------------------
# The board
# ---------------------------------------------------------------------------
def test_gen_tasks_renders_a_superseded_ticket_distinctly(tmp_path: Path) -> None:
    proj = _make_project(tmp_path, [_ticket(SUP), _ticket(DEP, depends_on=[SUP]), _ticket(OTHER)])
    assert _claim(proj, OTHER, "sess-o").returncode == 0
    assert _close(proj, "sess-o").returncode == 0
    assert _claim(proj, SUP, "sess-sup").returncode == 0
    assert _supersede(proj, "sess-sup").returncode == 0

    r = subprocess.run(
        ["python3", str(proj / ".claude" / "scripts" / "gen_tasks.py")],
        cwd=proj, capture_output=True, text=True, env=_env(proj, None), timeout=60,
    )
    assert r.returncode == 0, r.stderr
    md = (proj / "docs" / "TASKS.md").read_text()

    sup_block = md.split(f"### {SUP}:")[1].split("### ")[0]
    assert "`[superseded]`" in sup_block
    assert "`[resolved]`" not in sup_block
    assert "SUPERSEDED — NOT VERIFIED, NO GATE WAS RUN" in sup_block
    assert REASON in sup_block

    # The closed ticket is still rendered exactly as before.
    other_block = md.split(f"### {OTHER}:")[1].split("### ")[0]
    assert "`[resolved]`" in other_block
    assert "SUPERSEDED" not in other_block

    # And the unverified edge is marked where it is DEPENDED ON, not only on its
    # own entry.
    dep_block = md.split(f"### {DEP}:")[1].split("### ")[0]
    assert f"{SUP} (SUPERSEDED — unverified edge)" in dep_block


# ---------------------------------------------------------------------------
# The guard: a supersede is reachable only through the script
# ---------------------------------------------------------------------------
def _scope_guard(proj: Path, sid: str, path: Path) -> subprocess.CompletedProcess[str]:
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": str(path), "content": "forged"},
        "session_id": sid,
    }
    return subprocess.run(
        ["bash", str(proj / ".claude" / "hooks" / "scope_guard.sh")],
        input=json.dumps(payload), cwd=proj, capture_output=True, text=True,
        env=_env(proj, sid), timeout=60,
    )


def test_absolute_guard_still_denies_forging_a_superseded_record(tmp_path: Path) -> None:
    """The new terminal state adds a new thing worth forging: a record that
    retires a ticket without doing it. `.claude/evidence/**` is ABSOLUTE, so this
    must be denied even when the claimed ticket's own scope names the path --
    the yield-to-scope rule that PROTECTED has must not apply here."""
    proj = _make_project(
        tmp_path,
        [_ticket(SUP, scope=[".claude/evidence/**", f"src/{SUP}/**"])],
        files={".claude/evidence/.keep": ""},
    )
    sid = "sess-forger"
    assert _claim(proj, SUP, sid).returncode == 0

    result = _scope_guard(proj, sid, _evidence_path(proj, SUP))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    out = payload["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "superseded" in out["permissionDecisionReason"].lower()
    assert not _evidence_path(proj, SUP).exists()


# ---------------------------------------------------------------------------
# The task-list mirror
# ---------------------------------------------------------------------------
def _taskgate(proj: Path, subject: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(proj / ".claude" / "hooks" / "task_gate.sh")],
        input=json.dumps({"hook_event_name": "TaskCompleted", "task": {"subject": subject}}),
        cwd=proj, capture_output=True, text=True, env=_env(proj, None), timeout=60,
    )


def test_taskgate_allows_a_superseded_ticket_through_but_names_it(tmp_path: Path) -> None:
    """The mirror may close a ticket that reached a REAL terminal state -- if it
    could not, the pressure to fake a receipt just to clear the board would come
    straight back. It does not get to do so silently."""
    proj = _make_project(tmp_path, [_ticket(SUP)])

    blocked = _taskgate(proj, f"{SUP}: do the thing")
    assert blocked.returncode == 2, "no terminal record at all must still block"

    assert _claim(proj, SUP, "sess-sup").returncode == 0
    assert _supersede(proj, "sess-sup").returncode == 0

    allowed = _taskgate(proj, f"{SUP}: do the thing")
    assert allowed.returncode == 0
    assert "SUPERSEDED" in allowed.stderr
    assert "not verified" in allowed.stderr
