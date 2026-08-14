"""T-13 adversarial finding #1: .claude/hooks/claim.sh is the production
writer that actually records a session-scoped claim — ticket id +
CLAUDE_SESSION_ID + UTC timestamp, all three, always — closing the gap
where nothing shipped by T-13 ever produced one. See claim.sh's own header
for the full rationale and claim_lookup.py for how the record is
interpreted.

Every test drives the REAL claim.sh as a subprocess (see
conftest.run_claim_sh) and inspects the resulting file directly, rather
than trusting a test-only helper.
"""

from __future__ import annotations

import datetime
import json
import subprocess
from pathlib import Path

from .conftest import CLAIM_LOOKUP_PATH, run_claim_sh

SESSION_A = "session-aaaa-1111-current"


def _at_path(project_dir: Path) -> Path:
    return project_dir / ".claude" / "active-ticket"


def _owned(claims_file: Path, session: str) -> str:
    result = subprocess.run(
        ["python3", str(CLAIM_LOOKUP_PATH), str(claims_file), "--mode", "owned", "--session", session],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    return result.stdout


def test_first_claim_creates_one_well_formed_record(project: Path) -> None:
    result = run_claim_sh(project, "T-5", session_id=SESSION_A)
    assert result.returncode == 0, result.stderr

    at_path = _at_path(project)
    lines = at_path.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["ticket"] == "T-5"
    assert record["session"] == SESSION_A
    assert isinstance(record["ts"], str) and record["ts"]


def test_timestamp_is_a_real_parseable_utc_iso8601_string(project: Path) -> None:
    run_claim_sh(project, "T-5", session_id=SESSION_A)
    record = json.loads(_at_path(project).read_text().splitlines()[0])
    ts = record["ts"]
    assert ts.endswith("Z"), f"expected a UTC 'Z'-suffixed timestamp, got {ts!r}"
    # Must actually parse as a real point in time close to "now".
    parsed = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    assert abs((now - parsed).total_seconds()) < 60


def test_second_claim_appends_without_disturbing_the_first_line(project: Path) -> None:
    """Production-path proof of the append-only property (T-13 adversarial
    finding #3): unlike a test helper's own 'a'-mode file open, this
    exercises the REAL writer end to end.
    """
    run_claim_sh(project, "T-5", session_id=SESSION_A)
    at_path = _at_path(project)
    first_line = at_path.read_text()

    run_claim_sh(project, "T-6", session_id="session-bbbb-2222-observer")
    content = at_path.read_text()
    lines = content.splitlines(keepends=True)

    assert len(lines) == 2
    assert lines[0] == first_line, "the first claim.sh call's line must survive byte-identical"
    second = json.loads(lines[1])
    assert second["ticket"] == "T-6"
    assert second["session"] == "session-bbbb-2222-observer"


def test_refuses_to_write_with_no_identifiable_session(project: Path) -> None:
    at_path = _at_path(project)
    assert not at_path.exists()

    result = run_claim_sh(project, "T-5", session_id=None)

    assert result.returncode != 0
    assert not at_path.exists(), "must not create a file for an unattributed claim"


def test_release_marker_writes_null_ticket(project: Path) -> None:
    run_claim_sh(project, "T-5", session_id=SESSION_A)
    result = run_claim_sh(project, "--release", session_id=SESSION_A)
    assert result.returncode == 0, result.stderr

    lines = _at_path(project).read_text().splitlines()
    assert len(lines) == 2
    record = json.loads(lines[-1])
    assert record["ticket"] is None
    assert record["session"] == SESSION_A


def test_written_claim_round_trips_through_claim_lookup_owned_mode(project: Path) -> None:
    run_claim_sh(project, "T-5", session_id=SESSION_A)
    assert _owned(_at_path(project), SESSION_A) == "T-5"
    assert _owned(_at_path(project), "some-other-session") == ""


def test_explicit_session_argument_overrides_env_var(project: Path) -> None:
    result = run_claim_sh(
        project, "T-5", session_id="explicit-override-session",
        env_extra={"CLAUDE_CODE_SESSION_ID": "should-not-be-used"},
    )
    assert result.returncode == 0, result.stderr
    record = json.loads(_at_path(project).read_text().splitlines()[0])
    assert record["session"] == "explicit-override-session"
