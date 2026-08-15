"""Shared fixtures for driving the REAL .claude/hooks/*.sh guards as
subprocesses, exactly the way Claude Code's hook machinery does: every hook
command in .claude/settings.json is written as
``${CLAUDE_PROJECT_DIR}/.claude/hooks/<name>.sh``, so the real machinery
always resolves and executes the COPY living inside CLAUDE_PROJECT_DIR, not
some fixed path in this source checkout. These fixtures now mirror that: the
synthetic project built by ``project``/``make_project`` contains its OWN
copies of ``.claude/hooks/`` and ``.claude/scripts/``.

T-31 (v2 harness migration) replaced the entire v1 harness in commit
c44f9af ("cc-factory: harness sync") -- see T31-brief.md for the full
migration narrative. DELETED: .claude/hooks/claim.sh, claim_lookup.py,
verify_gate.sh, and the single append-only .claude/active-ticket ledger.
REPLACED BY: .claude/scripts/harness_lib.py (the entire contract -- read it
end to end), a 4-line .claude/scripts/claim.sh wrapper, and four 2-line
.claude/hooks/*.sh shims that all just exec
``python3 "${CLAUDE_PROJECT_DIR}/.claude/scripts/harness_lib.py" hook-<kind>``.

Because those shims need harness_lib.py to exist INSIDE
CLAUDE_PROJECT_DIR, a synthetic project that only ever contained
docs/tickets.json (the v1 shape) now makes EVERY hook invocation die with
"python3: can't open .../.claude/scripts/harness_lib.py". ``project`` /
``make_project`` below fix that by copying the real .claude/scripts/ and
.claude/hooks/ into the synthetic project, and by making it a real git
repo (harness_lib.py's claim/close verbs run `git add`/`git commit` and
read `git ls-files`/`git diff` against a known HEAD -- v1 never touched
git at all).

Every subprocess.run below explicitly sets CLAUDE_PROJECT_DIR to the
synthetic tmp_path project and strips CLAUDE_CODE_SESSION_ID from the
inherited environment by default (re-added only where a helper's own
parameter asks for a specific session identity) -- this session's own
CLAUDE_CODE_SESSION_ID is a LIVE claim on T-31 in the real repo right now
(.claude/claims/<that id>.json), and no test here may ever see or touch
it. ``_assert_real_repo_untouched`` is the trip-wire that proves it: it
snapshots the real repo's .claude/claims/, .claude/evidence/ and git HEAD
once at import time and re-checks the snapshot after every subprocess call
in this file.

Four hook contracts are exercised here (see harness_lib.py's own cmd_hook
for the authoritative version of each):
  * scope_guard.sh  (hook-scope)  -- PreToolUse[Edit|Write]. ALWAYS exits
    0; a deny is JSON on stdout (hookSpecificOutput.permissionDecision).
    Helpers: run_hook / decision / expect.
  * stop_guard.sh   (hook-stop)   -- Stop. ALWAYS exits 0; a block is JSON
    on stdout ({"decision":"block",...}), a differently-shaped payload
    from scope_guard.sh's. Helpers: run_stop_hook / stop_decision.
  * task_gate.sh    (hook-taskgate) -- PreToolUse[TaskUpdate] and
    TaskCompleted. Signals via EXIT CODE (0 = allow, 2 = block), stderr
    carries the reason. This is v1's verify_gate.sh's REPLACEMENT, not a
    renamed continuation of it -- see run_verify_hook's own docstring for
    exactly what changed. Helpers: run_verify_hook / verify_decision (kept
    for any caller still importing them; the tests that exercise this hook
    belong to a different file per T31-brief.md, so this module does not
    itself add coverage here).

Claims (the replacement for v1's single, session-blind, append-only
.claude/active-ticket ledger): one JSON file per session at
.claude/claims/<session_id>.json, written directly by harness_lib.py's
cmd_claim -- never through the Edit/Write tool, which is why
.claude/claims/** (and .claude/evidence/**) are listed in
harness_lib.PROTECTED and denied unconditionally by scope_guard.sh (see
test_scope_guard_append_only.py). ``write_claim`` below writes that file
shape directly so tests can set up "this session currently holds claim X"
without running the real claim.sh lifecycle end to end.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_HOOKS_DIR = REPO_ROOT / ".claude" / "hooks"
REAL_SCRIPTS_DIR = REPO_ROOT / ".claude" / "scripts"
REAL_TICKETS_PATH = REPO_ROOT / "docs" / "tickets.json"

# Paths *within a synthetic project*, resolved against project_dir at call
# time -- these are never absolute paths into the real repo.
SCOPE_HOOK_REL = Path(".claude/hooks/scope_guard.sh")
STOP_HOOK_REL = Path(".claude/hooks/stop_guard.sh")
TASKGATE_HOOK_REL = Path(".claude/hooks/task_gate.sh")
CLAIM_SH_REL = Path(".claude/scripts/claim.sh")

# Read once, read-only: every ticket id + scope list the real plan declares
# today. Tests derive coverage checks from this rather than a hand-copied
# constant, so a ticket added to the plan later shows up here automatically.
REAL_TICKETS: dict[str, Any] = json.loads(REAL_TICKETS_PATH.read_text())
REAL_TICKET_IDS: list[str] = [t["id"] for t in REAL_TICKETS["tickets"]]
REAL_SCOPES: dict[str, list[str]] = {
    t["id"]: t["scope"] for t in REAL_TICKETS["tickets"]
}

# Never used to bypass anything -- only to prove this file never mutates the
# real repo's live claim/evidence state or git history (see
# _assert_real_repo_untouched below; this session's own claim on T-31 lives
# under LIVE_CLAIMS_DIR right now).
LIVE_CLAIMS_DIR = REPO_ROOT / ".claude" / "claims"
LIVE_EVIDENCE_DIR = REPO_ROOT / ".claude" / "evidence"

# The fixed synthetic session id every scope-guard test uses unless it
# explicitly asks for a different one (e.g. to exercise "another session's
# claim"). v1's single global .claude/active-ticket had no session concept
# at all for scope_guard.sh; v2's claims are per-session files, so
# run_hook/expect need SOME session identity to write a claim file for.
SCOPE_TEST_SESSION = "scope-guard-test-session"


# ---------------------------------------------------------------------------
# Synthetic project construction
# ---------------------------------------------------------------------------
def _git(project_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=project_dir, capture_output=True, text=True, check=True,
    )


def _seed_harness_files(project_dir: Path) -> None:
    """Copy the real .claude/scripts/ and .claude/hooks/ into the synthetic
    project so its own hook shims can find harness_lib.py via
    ${CLAUDE_PROJECT_DIR} exactly like the real machinery does.
    """
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(REAL_SCRIPTS_DIR, project_dir / ".claude" / "scripts", ignore=ignore)
    shutil.copytree(REAL_HOOKS_DIR, project_dir / ".claude" / "hooks", ignore=ignore)


def _init_git(project_dir: Path) -> None:
    """Make project_dir a real git repo with one deterministic initial
    commit. harness_lib.py's claim/close verbs make real commits
    (ticket-start / ticket-close) and read `git ls-files` / `git diff`
    against a known HEAD (fingerprint/integrity/changed_since) -- a v1
    synthetic project never needed any of this, since v1's hooks were pure
    text parsers with no git dependency at all.
    """
    _git(project_dir, "init", "-q", "-b", "main")
    _git(project_dir, "config", "user.name", "hook-test")
    _git(project_dir, "config", "user.email", "hook-test@example.invalid")
    _git(project_dir, "config", "commit.gpgsign", "false")
    _git(project_dir, "add", "-A")
    _git(project_dir, "commit", "-q", "-m", "synthetic project seed", "--allow-empty")


def _build_project(tmp_path: Path, tickets_doc: dict[str, Any] | None) -> Path:
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".claude").mkdir(parents=True, exist_ok=True)
    if tickets_doc is None:
        shutil.copyfile(REAL_TICKETS_PATH, tmp_path / "docs" / "tickets.json")
    else:
        (tmp_path / "docs" / "tickets.json").write_text(json.dumps(tickets_doc))
    _seed_harness_files(tmp_path)
    _init_git(tmp_path)
    return tmp_path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A disposable CLAUDE_PROJECT_DIR seeded with the real docs/tickets.json,
    the real .claude/scripts/ + .claude/hooks/, and a fresh git history.

    No session holds a claim yet -- individual tests create/remove one via
    ``run_hook(..., active_ticket=...)`` / ``expect(..., ticket=...)`` /
    ``write_claim``.
    """
    return _build_project(tmp_path, None)


def make_project(tmp_path: Path, tickets_doc: dict[str, Any]) -> Path:
    """Build a disposable CLAUDE_PROJECT_DIR around a SYNTHETIC tickets.json.

    Unlike the ``project`` fixture (which seeds the real docs/tickets.json),
    this lets a test construct a scope shape the real plan does not happen
    to declare today -- e.g. a bare single '*' wildcard, which no real
    ticket uses -- without touching or duplicating the real plan file. Still
    drives the real (synthetic-project-local) hook scripts exactly like
    ``run_hook``/``expect`` do.
    """
    return _build_project(tmp_path, tickets_doc)


class _Unset:
    """Sentinel type distinguishing 'leave this piece of state as-is' from
    an explicit ``None`` (delete/absent) or a concrete value.

    A plain ``object()`` sentinel typed as ``... | object`` collapses to
    ``object`` under mypy (``object`` is already a supertype of ``str`` and
    ``None``), which is why a bare-object sentinel fails to type-check even
    after both other branches are excluded: mypy can't narrow ``object``
    back down. A dedicated class narrows correctly via ``isinstance``.
    """


UNSET = _Unset()


# ---------------------------------------------------------------------------
# Hermeticity trip-wire: the real repo's live claim/evidence/git state must
# never move, no matter what any test in this package does.
# ---------------------------------------------------------------------------
def _real_git_head() -> str:
    git_dir = REPO_ROOT / ".git"
    head = (git_dir / "HEAD").read_text().strip()
    if head.startswith("ref:"):
        ref_path = git_dir / head.split(" ", 1)[1]
        return ref_path.read_text().strip() if ref_path.exists() else head
    return head


def _dir_snapshot(d: Path) -> tuple[tuple[str, int, int], ...]:
    if not d.is_dir():
        return ()
    return tuple(sorted((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in d.glob("*.json")))


def _real_state_fingerprint() -> tuple[Any, ...]:
    return (_dir_snapshot(LIVE_CLAIMS_DIR), _dir_snapshot(LIVE_EVIDENCE_DIR), _real_git_head())


_REAL_STATE_AT_IMPORT = _real_state_fingerprint()


def _assert_real_repo_untouched() -> None:
    """Every helper below points CLAUDE_PROJECT_DIR at a disposable
    tmp_path, so this should never fire -- it's the safety net HARD RULE 5
    requires: no test here may mutate the real repo's .claude/claims/,
    .claude/evidence/, or git history, since this very session holds a LIVE
    claim on T-31 there right now.
    """
    assert _real_state_fingerprint() == _REAL_STATE_AT_IMPORT, (
        "a hook test subprocess mutated the REAL repo's .claude/claims/, "
        ".claude/evidence/ or git HEAD -- CLAUDE_PROJECT_DIR must have leaked "
        "to the real repo instead of the synthetic tmp_path project"
    )


def _subprocess_env(project_dir: Path, env_extra: dict[str, str] | None = None) -> dict[str, str]:
    """Base environment for every hook/claim.sh subprocess: point
    CLAUDE_PROJECT_DIR at the synthetic project and strip whatever real
    CLAUDE_CODE_SESSION_ID this very session was started with -- callers
    that want a specific session id re-add it explicitly (via env_extra or
    a dedicated parameter), never by accident via inheritance.
    """
    env = dict(os.environ)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    if env_extra:
        env.update(env_extra)
    return env


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------
def write_claim(
    project_dir: Path,
    ticket: str | None,
    session: str,
    ts: str = "2026-08-14T00:00:00Z",
    *,
    note: str = "test claim",
    start_commit: str = "0" * 40,
    attempts: int = 0,
) -> None:
    """Write (or, when ``ticket`` is None, remove) ONE session's v2 claim
    file at project_dir/.claude/claims/<session>.json.

    v1 (pre-T-31): appended one JSONL record onto the single, session-blind,
    append-only .claude/active-ticket log; claim_lookup.py then resolved
    "this session's most recent claim" by scanning that shared log. v2 has
    no shared log at all -- one JSON file per session, named by session id,
    IS the entire claim record (harness_lib.py's session_claim/cmd_claim).
    This now writes that file directly, matching the
    {"ticket","session","note","start_commit","attempts","ts"} shape
    cmd_claim itself writes.

    ``ticket=None`` removes the file -- modelling what cmd_close/cmd_release
    actually do when a claim resolves -- rather than appending a v1-style
    "release" marker record; there is no log left to append one onto.
    """
    path = project_dir / ".claude" / "claims" / f"{session}.json"
    if ticket is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ticket": ticket,
        "session": session,
        "note": note,
        "start_commit": start_commit,
        "attempts": attempts,
        "ts": ts,
    }
    path.write_text(json.dumps(record))


def run_claim_sh(
    project_dir: Path,
    ticket: str,
    *,
    note: str = "test claim",
    subcommand: str = "claim",
    session_id: str | None | _Unset = UNSET,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke the REAL (synthetic-project-local) .claude/scripts/claim.sh
    (v2) as a subprocess.

    v1's claim.sh took the ticket id as its own single positional argument
    plus an optional explicit session-id argument -- there was no
    subcommand. v2's claim.sh is a 4-line dispatcher --
    ``exec python3 harness_lib.py "${1:-status_board}" "${@:2}"`` -- over
    harness_lib.py's claim/close/release verbs, so driving a claim now
    means ``claim.sh claim <ticket> <note>``. ``subcommand`` defaults to
    "claim" (the common case every v1 caller wanted); pass "close" or
    "release" to drive the other two verbs -- ``ticket`` is ignored for
    those, since v2 resolves close/release from the CALLING SESSION's own
    claim file, never from an argument.

    ``session_id``:
      - ``UNSET`` (default): leave $CLAUDE_CODE_SESSION_ID stripped (see
        ``_subprocess_env``) so harness_lib.py falls back to its own
        manual-<ppid> id, UNLESS overridden via ``env_extra``. v2 has no
        explicit session-id CLI argument at all -- session identity comes
        only from $CLAUDE_CODE_SESSION_ID -- so this no longer matches v1's
        "pass an explicit override argument" behaviour.
      - ``None``: identical to UNSET for v2 (kept only so old call sites
        that pass ``session_id=None`` keep working); session identity is
        never read from the payload for claim.sh.
      - a string: set as $CLAUDE_CODE_SESSION_ID for the subprocess.
    """
    if subcommand == "claim":
        args = ["bash", str(project_dir / CLAIM_SH_REL), "claim", ticket, note]
    elif subcommand == "close":
        args = ["bash", str(project_dir / CLAIM_SH_REL), "close"]
    elif subcommand == "release":
        args = ["bash", str(project_dir / CLAIM_SH_REL), "release", note]
    else:
        raise ValueError(f"unknown subcommand: {subcommand!r}")

    env = _subprocess_env(project_dir, env_extra)
    if isinstance(session_id, str):
        env["CLAUDE_CODE_SESSION_ID"] = session_id

    result = subprocess.run(args, capture_output=True, text=True, env=env, timeout=20)
    _assert_real_repo_untouched()
    return result


# ---------------------------------------------------------------------------
# scope_guard.sh (hook-scope)
# ---------------------------------------------------------------------------
def run_hook(
    project_dir: Path,
    file_path: str | None = None,
    *,
    tool_name: str = "Edit",
    active_ticket: str | None | _Unset = UNSET,
    session_id: str = SCOPE_TEST_SESSION,
    env_extra: dict[str, str] | None = None,
    write_content: str | _Unset = UNSET,
    old_string: str = "x",
    new_string: str = "y",
    replace_all: bool = False,
    notebook_path: str | None = None,
    new_source: str = "print(1)",
    tool_input_override: dict[str, Any] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke the real (synthetic-project-local) scope_guard.sh with a
    synthetic PreToolUse event.

    v1's ``active_ticket`` wrote/deleted/overwrote a single free-text
    project_dir/.claude/active-ticket file that WAS the claim, and
    scope_guard.sh consulted it with no session concept at all. v2 has no
    such file: "this session's current claim" is
    project_dir/.claude/claims/<session_id>.json (harness_lib.session_claim),
    resolved from the EVENT PAYLOAD's own "session_id" field -- so this now
    writes that JSON claim file AND stamps the payload's session_id to
    match, using a fixed synthetic session id (``SCOPE_TEST_SESSION``) by
    default so every existing single-session call site keeps behaving
    exactly as it did under v1.

    ``active_ticket``:
      - ``UNSET`` (default): leave project_dir/.claude/claims/<session_id>.json
        as-is (absent unless a previous call in the same test wrote it).
      - ``None``: remove that claim file if present (the no-claim case --
        v2's no-claim case is UNCONSTRAINED outside PROTECTED paths, not
        fail-closed; see test_scope_guard.py's claim-state section for the
        v1->v2 flip and why).
      - a string: write that claim file naming this ticket id verbatim --
        "" and "   \\n" exercise a claim naming a garbage/unmatched ticket
        id (there is no free-text parse step left to fail; harness_lib.py's
        ``ticket(id)`` simply returns None for anything not in
        docs/tickets.json, same as an id that's merely misspelled).

    ``session_id``: which session's claim file gets checked, and the
    payload's own "session_id" field. Defaults to a fixed constant so
    single-session tests need not think about it; pass a different value to
    exercise "another session's claim" (v2's per-session claim files
    replace v1's single global claim, so that concept barely existed
    before -- see test_scope_guard_append_only.py's
    test_protected_regardless_of_whose_claim_is_active).

    ``write_content`` (Write tool only): the literal ``tool_input.content``
    to send. ``UNSET`` (default) sends the original fixed
    "synthetic-content" every non-content-sensitive test relies on.
    ``old_string``/``new_string``/``replace_all`` (Edit tool only) default
    to fixed placeholders ("x"/"y") for the same reason -- v2's guard never
    inspects any of these fields at all (PROTECTED/scope are pure path
    matches), but building a well-formed Edit/Write tool_input still needs
    them.

    ``notebook_path``/``new_source`` (NotebookEdit tool only, T-27
    acceptance 3): NotebookEdit carries its path under
    ``tool_input.notebook_path``, not ``file_path`` -- pass ``notebook_path``
    (instead of the ``file_path`` positional) when ``tool_name="NotebookEdit"``.
    scope_guard_prep.py (the T-27 hardening layer in front of
    harness_lib.py's hook-scope) normalises this into ``file_path`` before
    the guard runs, so a NotebookEdit payload is judged against ticket scope
    by ``notebook_path`` exactly as an Edit/Write payload is by
    ``file_path``.

    ``tool_input_override`` (T-27 acceptance 2): when given, used AS THE
    ENTIRE ``tool_input`` verbatim, bypassing every shape-building branch
    below (including the NotebookEdit one) -- the only way to build a
    payload this helper's normal construction can't otherwise express, e.g.
    an Edit/Write/NotebookEdit with no path key of any kind, to exercise the
    pathless-payload deny-by-default guard.
    """
    if not isinstance(active_ticket, _Unset):
        write_claim(project_dir, active_ticket, session_id)

    tool_input: dict[str, Any]
    if tool_input_override is not None:
        tool_input = tool_input_override
    elif tool_name == "NotebookEdit":
        tool_input = {"notebook_path": notebook_path, "new_source": new_source, "cell_type": "code"}
    elif tool_name == "Write":
        content = "synthetic-content" if isinstance(write_content, _Unset) else write_content
        tool_input = {"file_path": file_path, "content": content}
    else:
        tool_input = {
            "file_path": file_path,
            "old_string": old_string,
            "new_string": new_string,
        }
        if replace_all:
            tool_input["replace_all"] = True

    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "session_id": session_id,
    }

    env = _subprocess_env(project_dir, env_extra)

    result = subprocess.run(
        ["bash", str(project_dir / SCOPE_HOOK_REL)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    _assert_real_repo_untouched()
    return result


def decision(result: subprocess.CompletedProcess[str]) -> str:
    """Reduce a scope_guard.sh invocation to "allow" or "deny".

    The hook's contract (harness_lib.py's cmd_hook("scope")): ALWAYS exit
    0; a deny is a JSON object on stdout, an allow is silence. Anything
    else is a bug in the hook or in the test, not a valid decision, so it
    is not swallowed.
    """
    assert result.returncode == 0, (
        f"scope_guard.sh must always exit 0 (deny is expressed in JSON, not "
        f"exit code); got {result.returncode}. stderr={result.stderr!r}"
    )
    stdout = result.stdout.strip()
    if not stdout:
        return "allow"
    payload = json.loads(stdout)
    output = payload["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    decided = output["permissionDecision"]
    assert decided == "deny", f"unexpected permissionDecision: {decided!r}"
    reason = output.get("permissionDecisionReason", "")
    assert reason and isinstance(reason, str), "a deny must carry a human-readable reason"
    return "deny"


def expect(
    project_dir: Path,
    *,
    ticket: str | None | _Unset,
    file_path: str | None = None,
    want: str,
    tool_name: str = "Edit",
    session_id: str = SCOPE_TEST_SESSION,
    env_extra: dict[str, str] | None = None,
    notebook_path: str | None = None,
) -> None:
    """Assert the hook's decision for one (ticket claim, path) pair.

    ``ticket`` is forwarded to ``run_hook``'s ``active_ticket`` verbatim, so
    it accepts the same three shapes (leave-as-is / delete / overwrite).

    ``notebook_path`` (T-27): pass instead of ``file_path`` when
    ``tool_name="NotebookEdit"`` -- forwarded to ``run_hook`` verbatim, see
    its docstring.
    """
    result = run_hook(
        project_dir,
        file_path,
        tool_name=tool_name,
        active_ticket=ticket,
        session_id=session_id,
        env_extra=env_extra,
        notebook_path=notebook_path,
    )
    got = decision(result)
    shown_path = file_path if file_path is not None else notebook_path
    assert got == want, (
        f"ticket={ticket!r} path={shown_path!r} tool={tool_name}: "
        f"expected {want}, got {got} (stdout={result.stdout!r})"
    )


# ---------------------------------------------------------------------------
# stop_guard.sh (hook-stop)
# ---------------------------------------------------------------------------
def run_stop_hook(
    project_dir: Path,
    *,
    stop_hook_active: bool = False,
    session_id: str | None | _Unset = UNSET,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke the real (synthetic-project-local) stop_guard.sh with a
    synthetic Stop event payload.

    Unchanged in shape from v1: stop_guard.sh always resolved session
    identity from the Stop payload's own "session_id" field, never from an
    env var (harness_lib.py's cmd_hook("stop") still does exactly that --
    ``session_claim(p.get("session_id") or "")``), so this helper's
    contract barely moved. What changed is what a "claim" IS underneath:
    v1 resolved ownership by scanning the shared append-only log
    (claim_lookup.py's --strict resolution); v2 just looks up
    .claude/claims/<session_id>.json directly -- see write_claim.

    ``session_id``:
      - ``UNSET`` (default): omit the field from the payload entirely
        (CLAUDE_CODE_SESSION_ID is irrelevant here either way -- hook-stop
        never reads it, only the payload field).
      - ``None``: include the payload field as JSON null (harness_lib.py's
        ``p.get("session_id") or ""`` treats this the same as absent) --
        simulates "no session identifiable at all". Since an empty string
        can never match a real claim filename, this now ALWAYS allows; see
        test_stop_guard.py's docstring for the full v1->v2 divergence this
        exposes (v1 required a session-blind global fallback here).
      - a string: the payload's session_id field.
    """
    payload: dict[str, Any] = {
        "hook_event_name": "Stop",
        "stop_hook_active": stop_hook_active,
    }
    if not isinstance(session_id, _Unset):
        payload["session_id"] = session_id

    env = _subprocess_env(project_dir, env_extra)

    result = subprocess.run(
        ["bash", str(project_dir / STOP_HOOK_REL)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    _assert_real_repo_untouched()
    return result


def stop_decision(result: subprocess.CompletedProcess[str]) -> str:
    """Reduce a stop_guard.sh invocation to "allow" or "block".

    stop_guard.sh's contract: ALWAYS exit 0; a block is a top-level
    {"decision":"block","reason":...} object on stdout (NOT
    scope_guard.sh's hookSpecificOutput shape); silence is allow.
    """
    assert result.returncode == 0, (
        f"stop_guard.sh must always exit 0; got {result.returncode}. "
        f"stderr={result.stderr!r}"
    )
    stdout = result.stdout.strip()
    if not stdout:
        return "allow"
    payload = json.loads(stdout)
    decided = payload["decision"]
    assert decided == "block", f"unexpected decision: {decided!r}"
    reason = payload.get("reason", "")
    assert reason and isinstance(reason, str), "a block must carry a human-readable reason"
    return "block"


# ---------------------------------------------------------------------------
# task_gate.sh (hook-taskgate) -- v1's verify_gate.sh replacement.
# ---------------------------------------------------------------------------
def run_verify_hook(
    project_dir: Path,
    *,
    shape: str = "task_completed",
    subject: str | None = None,
    session_id: str | None | _Unset = UNSET,
    status: str = "completed",
    tool_name: str = "TaskUpdate",
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke the real (synthetic-project-local) task_gate.sh
    (hook-taskgate).

    v1's verify_gate.sh is GONE (deleted by commit c44f9af; see
    T31-brief.md) -- it ran a ticket's full verify command itself AND wrote
    .claude/evidence/<tid>.pass, gated by ownership of the (now also gone)
    .claude/active-ticket claim. v2 splits that responsibility in two:
    harness_lib.py's ``close`` verb (driven through claim.sh -- see
    run_claim_sh -- not a hook at all) does the actual verify-and-write-
    receipt work. task_gate.sh is a much narrower PreToolUse[TaskUpdate] /
    TaskCompleted guard: it only refuses to let a "T-<n>..." task be marked
    completed in the task list unless docs/tickets.json's ticket already
    has a receipt at .claude/evidence/<tid>.json. It never runs a verify
    command and never writes evidence itself.

    This helper (and verify_decision below) are kept, reimplemented against
    hook-taskgate, purely so their NAMES survive for any caller still
    importing them -- per T31-brief.md the tests that actually exercise
    this hook belong to a different file owned by a different agent, so
    this module does not itself add coverage here.

    ``shape``:
      - "task_completed": the TaskCompleted event shape -- a top-level
        "subject" field (harness_lib.py's cmd_hook("taskgate") also
        accepts task.subject, but a top-level field is simplest and
        equally valid).
      - "pretooluse": PreToolUse[TaskUpdate] shape (tool_name/tool_input;
        tool_input carries "subject" and "status").
    ``session_id``: accepted only for call-signature compatibility --
    hook-taskgate's decision never depends on session identity at all (it
    only ever reads the repo-global ``receipt(ticket_id)``), unlike
    run_claim_sh where it's load-bearing.
    """
    payload: dict[str, Any] = {}
    if shape == "task_completed":
        payload["hook_event_name"] = "TaskCompleted"
        if subject is not None:
            payload["subject"] = subject
    elif shape == "pretooluse":
        payload["hook_event_name"] = "PreToolUse"
        payload["tool_name"] = tool_name
        tool_input: dict[str, Any] = {"status": status}
        if subject is not None:
            tool_input["subject"] = subject
        payload["tool_input"] = tool_input
    else:
        raise ValueError(f"unknown shape: {shape!r}")

    if not isinstance(session_id, _Unset):
        payload["session_id"] = session_id

    env = _subprocess_env(project_dir, env_extra)

    result = subprocess.run(
        ["bash", str(project_dir / TASKGATE_HOOK_REL)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    _assert_real_repo_untouched()
    return result


def verify_decision(result: subprocess.CompletedProcess[str]) -> str:
    """Reduce a task_gate.sh (hook-taskgate) invocation to "allow" or
    "block".

    Same exit-code contract v1's verify_gate.sh used (0 = allow, 2 = block,
    stderr carries the reason on a block) -- harness_lib.py kept that
    convention when task_gate.sh replaced it.
    """
    if result.returncode == 0:
        return "allow"
    assert result.returncode == 2, (
        f"task_gate.sh must exit 0 (allow) or 2 (block); got "
        f"{result.returncode}. stderr={result.stderr!r}"
    )
    assert result.stderr.strip(), "a block must carry a human-readable stderr reason"
    return "block"
