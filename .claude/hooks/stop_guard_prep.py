#!/usr/bin/env python3
"""T-27 fix layered in front of harness_lib.py's hook-stop.

Lives in .claude/hooks/ (NOT harness_lib.py, out of T-27's scope). See
stop_guard.sh's header and .claude/NEEDS_HUMAN.md ("T-27 has three real
defects") for the full narrative.

harness_lib.py's cmd_hook("stop") is:

    if p.get("stop_hook_active"): return "", 0
    c = session_claim(p.get("session_id") or "")
    if not c: return "", 0
    ... block, naming c["ticket"] ...

A missing/None session_id resolves to "", which can never match a real
per-session claim filename, so this silently ALLOWS an unidentifiable
session to stop -- even one that is, under some name this payload doesn't
reveal, mid-claim. v1's stop guard failed CLOSED in exactly this situation;
this is a security-relevant narrowing introduced by the v1->v2 migration.

This script intercepts before harness_lib.py ever runs: no usable
session_id BLOCKS, naming the ambiguity -- unless stop_hook_active is set,
which must still take absolute priority (it is Claude Code's own
infinite-loop guard: this Stop hook already blocked once for this stop
attempt, so blocking again would leave the session permanently unable to
stop). A session that presents a real, non-empty session_id and genuinely
holds no claim is untouched by this check -- harness_lib.py's own no-claim
branch still allows it, exactly as before.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys


def _block(reason: str) -> None:
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        # Fail-closed on a payload we can't even parse: can't tell whether
        # this session holds a claim, so don't silently let it stop.
        _block(
            f"stop guard infra failure: could not parse the Stop payload ({e}) -- "
            "failing closed rather than silently allowing"
        )
        return

    if not payload.get("stop_hook_active"):
        sid = payload.get("session_id")
        if not (isinstance(sid, str) and sid.strip()):
            _block(
                "stop guard: this Stop payload carries no usable session_id, so "
                "whether this session holds an open ticket claim cannot be determined "
                "-- failing closed. If this session genuinely holds no claim, that's a "
                "session-identity/harness problem to report, not a real block to route "
                "around."
            )
            return
    # else: stop_hook_active loop-guard bypass -- forward unconditionally, exactly
    # like harness_lib.py's own first check would.

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    lib = os.path.join(project_dir, ".claude", "scripts", "harness_lib.py")
    try:
        result = subprocess.run(
            [sys.executable, lib, "hook-stop"],
            input=raw,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception as e:
        _block(
            f"stop guard infra failure: could not run harness_lib.py hook-stop ({e}) "
            "-- failing closed rather than silently allowing"
        )
        return

    if result.returncode != 0:
        _block(
            f"stop guard infra failure: harness_lib.py hook-stop exited "
            f"{result.returncode} instead of producing a decision -- failing closed "
            f"rather than silently allowing (stderr: {result.stderr.strip()[:300]})"
        )
        return

    out = result.stdout
    if out:
        sys.stdout.write(out if out.endswith("\n") else out + "\n")


if __name__ == "__main__":
    main()
