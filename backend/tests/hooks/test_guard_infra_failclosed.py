"""T-27 acceptance 1: if the guard helper cannot run at all (python3
absent, or the delegate it execs into erroring), the result must be a
DENY naming the infra failure -- never a silent allow.

Before this ticket, scope_guard.sh was a bare `exec python3
harness_lib.py hook-scope`: with python3 missing, that line failed with a
shell "command not found" and scope_guard.sh exited non-zero, producing NO
permissionDecision JSON at all. Claude Code's PreToolUse contract treats a
hook that produces no decision as an implicit allow -- exactly the silent
allow this ticket closes. The fix (in .claude/hooks/scope_guard.sh and the
new .claude/hooks/scope_guard_prep.py it execs into, both in T-27's scope
-- harness_lib.py is not) checks `command -v python3` in bash BEFORE ever
trying to run anything written in python3, and separately fails closed if
harness_lib.py itself can't be launched or exits non-zero.

"python3 absent" is simulated via PATH manipulation, per the acceptance's
own wording: subprocess.run's PATH lookup for a bare executable name
("bash") honours the `env` dict passed to it, not the real process's PATH
(verified independently before writing these tests), so a PATH containing
ONLY a symlink to the real bash -- no python3 anywhere on it -- makes the
outer `bash scope_guard.sh` invocation succeed while `command -v python3`
inside it genuinely fails, exactly like a real python3-less machine.
"erroring" is simulated by corrupting/removing the SYNTHETIC project's own
copy of harness_lib.py -- the real repo's is never touched.

stop_guard.sh received the identical hardening pattern for consistency
(not itself named by acceptance 1's text, which is about the scope guard's
realpath helper), so it is exercised here too.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .conftest import decision, run_hook, run_stop_hook, stop_decision


def _path_without_python3(tmp_path: Path) -> str:
    """A PATH value containing bash (so `["bash", hook.sh]` subprocess
    invocations still resolve) but with no python3 reachable anywhere on
    it. A PATH stripped to nothing would also make bash itself
    unresolvable, which is not the scenario under test (see module
    docstring).
    """
    jail = tmp_path / "path_without_python3"
    jail.mkdir(parents=True, exist_ok=True)
    bash_real = shutil.which("bash")
    assert bash_real, "bash must be resolvable on this machine to build the PATH jail"
    (jail / "bash").symlink_to(bash_real)
    return str(jail)


# ---------------------------------------------------------------------------
# scope_guard.sh
# ---------------------------------------------------------------------------
def test_scope_guard_denies_with_infra_reason_when_python3_is_absent(
    project: Path, tmp_path: Path,
) -> None:
    result = run_hook(
        project, str(project / "README.md"), active_ticket=None,
        env_extra={"PATH": _path_without_python3(tmp_path)},
    )
    assert decision(result) == "deny"
    payload = json.loads(result.stdout)
    reason = payload["hookSpecificOutput"]["permissionDecisionReason"].lower()
    assert "python3" in reason
    assert "fail" in reason  # "failing closed"


def test_scope_guard_still_exits_zero_when_python3_is_absent(
    project: Path, tmp_path: Path,
) -> None:
    """The hook's contract (see conftest.decision's own assertion) is to
    ALWAYS exit 0 and express a deny in JSON -- infra-failure handling must
    not itself break that by exiting non-zero, or Claude Code has nothing
    to read a decision from.
    """
    result = run_hook(
        project, str(project / "README.md"), active_ticket=None,
        env_extra={"PATH": _path_without_python3(tmp_path)},
    )
    assert result.returncode == 0


def test_scope_guard_denies_when_harness_lib_is_missing(project: Path) -> None:
    """The "or erroring" half of acceptance 1: python3 itself is present
    and fine, but harness_lib.py (which scope_guard_prep.py subprocesses
    into) is gone from this SYNTHETIC project -- must still be a named
    deny, not a silent allow or an unhandled crash.
    """
    (project / ".claude" / "scripts" / "harness_lib.py").unlink()
    result = run_hook(project, str(project / "README.md"), active_ticket=None)
    assert decision(result) == "deny"
    payload = json.loads(result.stdout)
    reason = payload["hookSpecificOutput"]["permissionDecisionReason"].lower()
    assert "harness_lib.py" in reason


def test_scope_guard_denies_when_harness_lib_crashes(project: Path) -> None:
    """harness_lib.py present but broken -- hook-scope exits nonzero
    instead of producing a decision. Must still fail closed, not crash
    scope_guard_prep.py itself into a silent allow.
    """
    lib = project / ".claude" / "scripts" / "harness_lib.py"
    lib.write_text("import sys\nsys.exit(1)\n")
    result = run_hook(project, str(project / "README.md"), active_ticket=None)
    assert decision(result) == "deny"
    payload = json.loads(result.stdout)
    reason = payload["hookSpecificOutput"]["permissionDecisionReason"].lower()
    assert "exited" in reason


# ---------------------------------------------------------------------------
# stop_guard.sh -- same hardening pattern, exercised for consistency.
# ---------------------------------------------------------------------------
def test_stop_guard_blocks_with_infra_reason_when_python3_is_absent(
    project: Path, tmp_path: Path,
) -> None:
    result = run_stop_hook(
        project, session_id="some-session",
        env_extra={"PATH": _path_without_python3(tmp_path)},
    )
    assert stop_decision(result) == "block"
    payload = json.loads(result.stdout)
    reason = payload["reason"].lower()
    assert "python3" in reason
    assert "fail" in reason


def test_stop_guard_still_exits_zero_when_python3_is_absent(
    project: Path, tmp_path: Path,
) -> None:
    result = run_stop_hook(
        project, session_id="some-session",
        env_extra={"PATH": _path_without_python3(tmp_path)},
    )
    assert result.returncode == 0
