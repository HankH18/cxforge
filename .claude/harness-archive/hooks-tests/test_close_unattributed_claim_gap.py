"""T-28 acceptance 1: "verify_gate refuses to run a gate or write evidence for a claim
record with no session attribution; the refusal names the offending record."

v1's `verify_gate.sh` (a bare-timestamp `.pass`-writing script gated on the shared,
session-blind `.claude/active-ticket` ledger) is gone -- deleted whole by commit c44f9af
("cc-factory: harness sync"). Its "run the gate, write the evidence" responsibility now
lives entirely in `harness_lib.py`'s `cmd_close`, reached ONLY through
`.claude/scripts/claim.sh close` (see `test_verify_gate.py`'s own docstring, part A: "via
`.claude/scripts/claim.sh close`"). Under v2 a claim's session attribution IS its
filename (`.claude/claims/<session_id>.json`), so v1's exact "bare line with no session"
shape cannot recur -- but an unattributed-in-substance or malformed claim record still
can: valid JSON missing the `ticket` key, a `session` field that disagrees with the
filename that is supposedly its attribution, unparseable JSON, an empty file, and a
`start_commit` that is missing or names a commit that no longer/never existed.

THE ORIGINAL FINDING, established empirically by the tests below: **no hook in this
repository's wiring ever runs before `cmd_close`, for any of the five cases, so no fix
confined to `.claude/hooks/**` could make this acceptance's "refuses ... names the
offending record" guarantee hold.** That structural fact is still true, and
`test_no_pretooluse_hook_matches_bash_tool_calls` pinned it until W0.2 removed
the hooks; retired to `.claude/harness-archive/` by ADR-019.

RESOLUTION: the gap was closed where it actually lived — in `harness_lib.py` itself,
under a direct, explicitly authorised patch from the project owner (the file is
PROTECTED, so no session could reach it through the normal lifecycle). All five cases
now produce clean, named refusals and mint nothing. Every test below was written to pin
the DEFECT and has been flipped to prove the FIX; each docstring records what it used to
assert. The most serious case is the last one: a `start_commit` naming no real commit
used to make the integrity check pass vacuously, silently disabling scope enforcement
for the whole close while still minting a receipt.

  * `.claude/settings.json`'s only `PreToolUse` matchers are `"Edit|Write|NotebookEdit"`
    and `"TaskUpdate"` (that was read from the real settings by a test now retired
    to `.claude/harness-archive/` per ADR-019 — it read the real
    file and pins this). `claim.sh close` runs through the **Bash** tool -- per
    `.claude/rules/harness-protocol.md` rule 2, "All ticket lifecycle goes through
    `.claude/scripts/claim.sh`" -- which is a tool name neither matcher names, so no
    `PreToolUse` hook is ever invoked before `cmd_close` runs, whether a human types the
    command at a shell or an agent session runs it via the Bash tool.
  * The one hook that unconditionally fires on a Bash call, `PostToolUse` ->
    `heartbeat.sh`, fires strictly AFTER the tool has already executed -- by which point
    `cmd_close` has already either crashed (having written nothing) or, in the two cases
    below where it does not crash, already minted a receipt. It structurally cannot
    "refuse to run a gate or write evidence"; that already happened by the time it sees
    anything.
  * Wiring a new `PreToolUse` matcher for `Bash` would close the gap, but
    `.claude/settings.json` is in `harness_lib.PROTECTED` and is not named in T-28's
    scope (`.claude/hooks/**`, `backend/tests/hooks/**`) -- `scope_guard.sh` denies that
    edit. `cmd_close` itself lives in `harness_lib.py`, also out of T-28's scope to edit.

CORRECTION (W7): earlier revisions of this docstring justified the paragraph above by
quoting an "escape valve" from T-28's own contract. **No such clause exists.** T-28's
fields are id, title, objective, refs, acceptance, verify, scope, depends_on, non_goals,
parallel_safe, status, and that sentence appears nowhere in docs/tickets.json, docs/,
.claude/rules/, or CLAUDE.md -- the only file that ever contained it was this one, which
attributed it to the plan. A permission the plan never granted was quoted as though it
had been. The quotation is removed rather than re-sourced, because there is nothing to
re-source it to.

The underlying reason still stands on its own and needs no citation: this file never
attempted a hook-layer fix because there is no hook in the path for one to live in. It
originally pinned the exact, currently-real behaviour of `cmd_close` for every named case
as durable, checked evidence, so that an authorised change to `harness_lib.py` itself had
a concrete regression suite to turn green. ALL SIX CASES HAVE NOW BEEN TURNED GREEN: each
test below still drives the real `cmd_close` through the real `claim.sh close` with a
genuinely malformed claim record and asserts on what it actually does, but the expected
"what it does" is now a clean, named refusal that writes nothing. Each test's own
docstring records verbatim what it used to assert and why that was the defect.

The last case to fall was case 2 (`session` disagrees with the filename), which was never
a crash at all -- it was a silent, fully successful close that minted a fingerprint-bound
receipt for a record whose attribution contradicted itself.

Self-contained like `test_verify_gate.py`: builds its own synthetic git project per test
in `tmp_path` (real git repo, hand-authored `docs/tickets.json`, copies of the real
`.claude/scripts/` and `.claude/scripts/claim.sh`) and never touches the real repo's
`.claude/claims/`, `.claude/evidence/`, `docs/tickets.json`, or git history -- this
session holds a LIVE claim on T-28 there right now.
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
HARNESS_LIB = REPO_ROOT / ".claude" / "scripts" / "harness_lib.py"
CLAIM_SH = REPO_ROOT / ".claude" / "scripts" / "claim.sh"
GEN_TASKS = REPO_ROOT / ".claude" / "scripts" / "gen_tasks.py"
SETTINGS_JSON = REPO_ROOT / ".claude" / "settings.json"

# Well outside the real plan's real range (T-0..T-31) so it can never collide.
TID = "T-9100"


@pytest.fixture(autouse=True)
def _never_touch_the_real_repos_live_harness_state() -> Iterator[None]:
    """Hermeticity guard, mirroring `test_verify_gate.py`'s own: this session holds a
    LIVE claim on T-28 in the real repo's `.claude/claims/` right now, and other
    sessions may concurrently claim/close other real tickets while this suite runs, so
    this cannot snapshot-and-diff the whole directory. Instead it asserts, before and
    after every test, that this file's synthetic ticket id never shows up as a real
    claim or receipt -- the one leak signature that would prove a subprocess call escaped
    its `tmp_path` project.
    """

    def _check() -> None:
        assert not (REPO_ROOT / ".claude" / "evidence" / f"{TID}.json").exists()
        claims_dir = REPO_ROOT / ".claude" / "claims"
        if claims_dir.is_dir():
            for p in claims_dir.glob("*.json"):
                try:
                    doc = json.loads(p.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                assert doc.get("ticket") != TID

    _check()
    yield
    _check()


# ---------------------------------------------------------------------------
# Synthetic-project builder + drivers (self-contained; see module docstring)
# ---------------------------------------------------------------------------
def _run_git(proj: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=proj, capture_output=True, text=True, check=True)


def _make_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "docs").mkdir(parents=True)
    (proj / ".claude" / "scripts").mkdir(parents=True)
    (proj / ".claude" / "claims").mkdir(parents=True)
    (proj / ".claude" / "evidence").mkdir(parents=True)
    (proj / "src").mkdir(parents=True)
    shutil.copy2(HARNESS_LIB, proj / ".claude" / "scripts" / "harness_lib.py")
    shutil.copy2(CLAIM_SH, proj / ".claude" / "scripts" / "claim.sh")
    shutil.copy2(GEN_TASKS, proj / ".claude" / "scripts" / "gen_tasks.py")
    tickets_doc = {
        "project": "close-unattributed-claim-gap-test",
        "tickets": [
            {
                "id": TID,
                "title": "synthetic ticket",
                "objective": "exercise cmd_close against a malformed claim record",
                "acceptance": ["synthetic acceptance criterion"],
                "scope": ["src/**"],
                "depends_on": [],
                "verify": "true",
            }
        ],
    }
    (proj / "docs" / "tickets.json").write_text(json.dumps(tickets_doc))
    (proj / "src" / "module.txt").write_text("a")

    _run_git(proj, "init", "-q", "-b", "main")
    _run_git(proj, "config", "user.email", "hooktest@example.com")
    _run_git(proj, "config", "user.name", "hook-test")
    _run_git(proj, "config", "commit.gpgsign", "false")
    _run_git(proj, "add", "-A")
    _run_git(proj, "commit", "-q", "-m", "initial")
    return proj


def _env(proj: Path, session_id: str) -> dict[str, str]:
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(proj)
    env["CLAUDE_CODE_SESSION_ID"] = session_id
    return env


def _claim_path(proj: Path, session_id: str) -> Path:
    return proj / ".claude" / "claims" / f"{session_id}.json"


def _evidence_path(proj: Path) -> Path:
    return proj / ".claude" / "evidence" / f"{TID}.json"


def _claim(proj: Path, session_id: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(proj / ".claude" / "scripts" / "claim.sh"), "claim", TID, "note"],
        cwd=proj, capture_output=True, text=True, env=_env(proj, session_id), timeout=30,
    )


def _close(proj: Path, session_id: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(proj / ".claude" / "scripts" / "claim.sh"), "close"],
        cwd=proj, capture_output=True, text=True, env=_env(proj, session_id), timeout=30,
    )


def _claim_then_corrupt(proj: Path, session_id: str, mutate: Any) -> None:
    """Run a real `claim.sh claim` to produce a genuinely well-formed claim record
    (real start_commit, real session id), then apply `mutate` to it -- so every case
    below tests a record that was legitimately created and then became malformed /
    misattributed, exactly the shape T-28 acceptance 1 is about, not a hand-forged
    fixture that never went through the real lifecycle.
    """
    claimed = _claim(proj, session_id)
    assert claimed.returncode == 0, claimed.stdout + claimed.stderr
    path = _claim_path(proj, session_id)
    record = json.loads(path.read_text())
    mutate(record)
    path.write_text(json.dumps(record))


# ---------------------------------------------------------------------------
# Structural proof: no PreToolUse hook can ever see a `claim.sh close` Bash call
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Case 1: valid JSON, no "ticket" key
# ---------------------------------------------------------------------------
def test_close_refuses_by_name_on_a_claim_record_missing_the_ticket_key(
    tmp_path: Path,
) -> None:
    """T-28 acceptance 1 — GAP NOW CLOSED. This test previously pinned the defect:
    `cmd_close` did `tid, start = c["ticket"], c["start_commit"]` with no guard, so a
    record with no `ticket` key raised an unhandled `KeyError` — a raw traceback, not
    "refuses to run a gate ... naming the offending record". The authorised harness
    patch added a required-field check, so the same corruption now produces a clean,
    named refusal and mints nothing.
    """
    proj = _make_project(tmp_path)
    sid = "session-no-ticket-key"
    _claim_then_corrupt(proj, sid, lambda record: record.pop("ticket"))

    result = _close(proj, sid)

    assert result.returncode == 1
    assert "Traceback (most recent call last)" not in result.stderr
    assert "missing required field" in result.stdout
    assert "ticket" in result.stdout
    assert not _evidence_path(proj).exists()


# ---------------------------------------------------------------------------
# Case 2: "session" field disagrees with the claim's own filename
# ---------------------------------------------------------------------------
def test_close_refuses_by_name_when_session_field_disagrees_with_filename(
    tmp_path: Path,
) -> None:
    """T-28 acceptance 1 — GAP NOW CLOSED, and this was the worst case in the file.

    This test previously pinned the defect (it was named
    `test_close_silently_mints_a_receipt_when_session_field_disagrees_with_filename`)
    and asserted `returncode == 0`, `"closed" in stdout`, a real evidence file on disk,
    and `receipt["session"] == sid`: `cmd_close` never read `c["session"]` at all, so a
    record whose internal attribution contradicted its own filename was not refused, not
    flagged, not even noticed. Unlike the crash cases either side of it, nothing
    signalled that a problem existed — a fingerprint-bound receipt was minted certifying
    work under an attribution the record itself denied.

    `harness_lib.claim_defects` now treats that contradiction as what it is: the FILENAME
    is the attribution every reader resolves ownership from, so a record that disagrees
    with it asserts two owners and the harness has nothing to adjudicate between them.
    The record is refused BY NAME (the path is printed), no gate runs, and nothing is
    written. Same fixture, same corruption, inverted expectation.
    """
    proj = _make_project(tmp_path)
    sid = "session-real-owner"
    _claim_then_corrupt(proj, sid, lambda record: record.__setitem__("session", "impostor"))

    result = _close(proj, sid)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Traceback (most recent call last)" not in result.stderr
    # "names the offending record": the claim file's own path, plus both contradicting
    # attributions, so a human can see exactly what disagreed with what.
    assert f".claude/claims/{sid}.json" in result.stdout
    assert "impostor" in result.stdout
    assert sid in result.stdout
    assert "closed" not in result.stdout
    assert not _evidence_path(proj).exists()


# ---------------------------------------------------------------------------
# Case 3: malformed / unparseable JSON
# ---------------------------------------------------------------------------
def test_close_refuses_by_name_on_unparseable_json(tmp_path: Path) -> None:
    """T-28 acceptance 1 — GAP NOW CLOSED. `session_claim` previously did a bare
    `json.load(f)`, so unparseable bytes raised an unhandled `JSONDecodeError`. The
    patch wraps the read and refuses by name instead, minting nothing.
    """
    proj = _make_project(tmp_path)
    sid = "session-bad-json"
    claimed = _claim(proj, sid)
    assert claimed.returncode == 0, claimed.stdout + claimed.stderr
    _claim_path(proj, sid).write_text("{not valid json")

    result = _close(proj, sid)

    assert result.returncode == 1
    assert "Traceback (most recent call last)" not in result.stderr
    assert "unreadable" in result.stdout
    assert not _evidence_path(proj).exists()


# ---------------------------------------------------------------------------
# Case 4: empty claim file
# ---------------------------------------------------------------------------
def test_close_refuses_by_name_on_an_empty_claim_file(tmp_path: Path) -> None:
    """T-28 acceptance 1 — GAP NOW CLOSED. An empty claim file is unparseable JSON and
    now takes the same clean refusal path. Kept as its own case because a truncated
    write is a distinct real-world corruption mode from malformed-but-non-empty content.
    """
    proj = _make_project(tmp_path)
    sid = "session-empty-claim"
    claimed = _claim(proj, sid)
    assert claimed.returncode == 0, claimed.stdout + claimed.stderr
    _claim_path(proj, sid).write_text("")

    result = _close(proj, sid)

    assert result.returncode == 1
    assert "Traceback (most recent call last)" not in result.stderr
    assert "unreadable" in result.stdout
    assert not _evidence_path(proj).exists()


# ---------------------------------------------------------------------------
# Case 5a: start_commit key missing
# ---------------------------------------------------------------------------
def test_close_refuses_by_name_on_a_claim_record_missing_start_commit(
    tmp_path: Path,
) -> None:
    """T-28 acceptance 1 — GAP NOW CLOSED. Same required-field check as the missing
    `ticket` case, applied to `start_commit`: a named refusal, not a raw `KeyError`.
    """
    proj = _make_project(tmp_path)
    sid = "session-no-start-commit"
    _claim_then_corrupt(proj, sid, lambda record: record.pop("start_commit"))

    result = _close(proj, sid)

    assert result.returncode == 1
    assert "Traceback (most recent call last)" not in result.stderr
    assert "missing required field" in result.stdout
    assert "start_commit" in result.stdout
    assert not _evidence_path(proj).exists()


# ---------------------------------------------------------------------------
# Case 5b: start_commit names a commit that does not exist
# ---------------------------------------------------------------------------
def test_close_refuses_when_start_commit_names_a_nonexistent_commit(
    tmp_path: Path,
) -> None:
    """The most serious of the five — GAP NOW CLOSED.

    `changed_since(commit)` shelled out to `git diff --name-only <commit>` and read
    `.stdout` without checking the return code. Against a `start_commit` naming no real
    commit, both `git diff` calls failed and printed nothing, so `changed_since`
    returned an EMPTY set — `integrity()` then found no out-of-scope files *by
    construction*, passed VACUOUSLY, and `cmd_close` went on to mint a receipt as
    though every file touched under the ticket had been in scope. A bogus
    `start_commit` did not merely go unrefused: it silently DISABLED scope enforcement
    for the entire close, which is the one thing a receipt is supposed to attest.

    The patch resolves `start_commit` up front and refuses if it does not exist, and
    `changed_since` now raises `IntegrityUnavailable` rather than reporting an
    unanswerable diff as "nothing changed" — an unevaluable integrity check can no
    longer be mistaken for a passing one.
    """
    proj = _make_project(tmp_path)
    sid = "session-bad-start-commit"
    _claim_then_corrupt(
        proj, sid, lambda record: record.__setitem__("start_commit", "deadbeef" * 5)
    )

    result = _close(proj, sid)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "does not resolve to a commit" in result.stdout
    assert not _evidence_path(proj).exists()
