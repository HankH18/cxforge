"""T-13 acceptance 3 (append-only, git-tracked claim log) enforced at the
GUARD level: scope_guard.sh's .claude/active-ticket protocol path now
requires every real Edit/Write there to be a pure append — existing content
preserved as an exact prefix of the result — rather than allowing it
unconditionally (T-13 adversarial findings #2/#3).

Every test drives the REAL scope_guard.sh as a subprocess (see
conftest.run_hook / conftest.decision), seeding the claim log directly via
plain file writes (never via conftest.write_claim's own 'a'-mode helper) so
these tests catch a REAL production regression, not merely a sabotaged test
helper — precisely the gap adversarial finding #3 identified in the
original test suite.

IMPORTANT: run_hook's ``active_ticket`` parameter defaults to ``UNSET``
("leave the file as-is"); every test below relies on that default so its
own ``_seed()`` call survives into the hook invocation — passing
``active_ticket=None`` would DELETE the seeded file before the hook ever
runs and silently defeat the test.
"""

from __future__ import annotations

from pathlib import Path

from .conftest import decision, run_hook

AT_REL = ".claude/active-ticket"


def _seed(project_dir: Path, content: str) -> Path:
    at_path = project_dir / ".claude" / "active-ticket"
    at_path.parent.mkdir(parents=True, exist_ok=True)
    at_path.write_text(content)
    return at_path


def test_write_that_only_appends_a_new_line_is_allowed(project: Path) -> None:
    _seed(project, "T-1\n")
    result = run_hook(
        project,
        str(project / AT_REL),
        tool_name="Write",
        write_content="T-1\nT-2\n",
    )
    assert decision(result) == "allow"


def test_write_that_discards_prior_content_is_denied(project: Path) -> None:
    """Reproduces T-13 adversarial findings #2/#3 exactly: seeding a real
    claim record, then performing the literal write CLAUDE.md's stale
    "write its ticket ID as the only line" instruction describes — a
    single new line, with no trace of what came before.
    """
    at_path = _seed(project, '{"ticket":"T-5","session":"sess-AAAA","ts":"t1"}\n')
    before = at_path.read_text()

    result = run_hook(
        project,
        str(project / AT_REL),
        tool_name="Write",
        write_content="T-14\n",
    )
    assert decision(result) == "deny"
    # And, unlike the pre-repair hook, the file on disk is provably
    # untouched by this attempt (this hook only ever DECIDES; it never
    # itself performs the write) — the prior claim record survives.
    assert at_path.read_text() == before


def test_write_of_the_very_first_claim_with_no_prior_file_is_allowed(project: Path) -> None:
    result = run_hook(
        project,
        str(project / AT_REL),
        tool_name="Write",
        active_ticket=None,  # explicitly ensure no file exists yet
        write_content="T-1\n",
    )
    assert decision(result) == "allow"


def test_edit_that_replaces_the_entire_existing_content_is_denied(project: Path) -> None:
    _seed(project, "T-5\n")
    result = run_hook(
        project,
        str(project / AT_REL),
        tool_name="Edit",
        old_string="T-5\n",
        new_string="T-14\n",
    )
    assert decision(result) == "deny"


def test_edit_that_modifies_an_earlier_line_in_place_is_denied(project: Path) -> None:
    """Not a full overwrite — a targeted in-place tamper of a SINGLE
    earlier line, with the rest of the log left alone. Still a violation:
    the earlier line no longer survives as an exact prefix.
    """
    _seed(
        project,
        '{"ticket":"T-1","session":"session-A","ts":"t1"}\n'
        '{"ticket":"T-2","session":"session-B","ts":"t2"}\n',
    )
    result = run_hook(
        project,
        str(project / AT_REL),
        tool_name="Edit",
        old_string='{"ticket":"T-1","session":"session-A","ts":"t1"}',
        new_string='{"ticket":"T-99","session":"session-A","ts":"t1"}',
    )
    assert decision(result) == "deny"


def test_edit_that_appends_via_a_tail_anchored_replace_is_allowed(project: Path) -> None:
    """A legitimate way to append via the Edit tool: anchor old_string on
    the tail of the current content and have new_string reproduce that tail
    plus new material after it.
    """
    _seed(project, "T-1\nT-2\n")
    result = run_hook(
        project,
        str(project / AT_REL),
        tool_name="Edit",
        old_string="T-2\n",
        new_string="T-2\nT-3\n",
    )
    assert decision(result) == "allow"


def test_edit_with_replace_all_that_would_alter_content_is_denied(project: Path) -> None:
    _seed(project, "T-1\nT-1\n")
    result = run_hook(
        project,
        str(project / AT_REL),
        tool_name="Edit",
        old_string="T-1\n",
        new_string="T-9\n",
        replace_all=True,
    )
    assert decision(result) == "deny"


def test_edit_with_no_matching_old_string_is_a_no_op_and_allowed(project: Path) -> None:
    """Regression guard for the pre-existing T-12 tests
    (test_claim_record_itself_stays_writable /
    test_claim_record_writable_even_with_no_prior_claim in
    test_scope_guard.py), which exercise Edit against .claude/active-ticket
    with placeholder old_string/new_string values that never match real
    content — those must keep working exactly as before.
    """
    _seed(project, "T-5\n")
    result = run_hook(
        project,
        str(project / AT_REL),
        tool_name="Edit",
        old_string="x",
        new_string="y",
    )
    assert decision(result) == "allow"
