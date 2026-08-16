"""T-13's append-only .claude/active-ticket ledger no longer exists — see
harness_lib.py end to end: there is no .claude/active-ticket concept left
anywhere in v2. Its replacement is not "a stricter append-only rule"; it is
unconditional: .claude/claims/** and .claude/evidence/** are both listed in
harness_lib.PROTECTED, so scope_guard.sh (routed through hook-scope ->
guard) denies EVERY Edit/Write there, for every session, whether or not
that session holds a claim, before any per-ticket scope check or claim
lookup even runs (decision-order step 3, ahead of the claim-lookup at
step 5/6 — read harness_lib.py's guard() itself for the decision order.
Earlier text cited a "T31-brief.md" summary here; no such file exists — W7).

Every test drives the REAL scope_guard.sh as a subprocess (see
conftest.run_hook / conftest.decision), seeding target files directly via
plain file writes (never via conftest.write_claim's structured helper) so
these tests catch a REAL production regression in guard()/PROTECTED
itself, not merely a sabotaged test helper — the same rationale the
pre-T-31 version of this file stated for T-13 adversarial finding #3.

v1 -> v2 coverage mapping. Every v1 test asserted the append-only ledger
tolerated a LEGITIMATE append and rejected an ILLEGITIMATE rewrite; v2
collapses that entire spectrum onto a single answer — deny, unconditionally
— regardless of what the write would have done to the file's content:

  * pure append                          (v1: allowed) -> now DENIED
    test_pure_append_is_now_denied
  * full discard-and-replace             (v1: denied)  -> still DENIED, and
    the file on disk is provably untouched (the guard only ever decides;
    it never performs a write itself)
    test_full_rewrite_is_denied_and_file_left_untouched
  * very first claim, no prior file      (v1: allowed) -> now DENIED — v2
    has no bootstrapping exception: there is no direct-Edit/Write path to a
    claim file AT ALL, only `claim.sh claim` may create one
    test_first_ever_write_with_no_prior_file_is_denied
  * Edit replacing the entire content    (v1: denied)  -> still DENIED
    test_edit_replacing_entire_content_is_denied
  * Edit tampering one earlier line      (v1: denied)  -> still DENIED
    test_edit_modifying_one_earlier_line_is_denied
  * Edit appending via a tail-anchored
    old_string/new_string pair           (v1: ALLOWED) -> now DENIED — the
    single biggest behavioural flip in this file: v2 does not simulate the
    edit's resulting content at all, so even a textbook-legitimate append
    is refused
    test_tail_anchored_append_edit_is_now_denied
  * Edit with replace_all=True           (v1: denied)  -> still DENIED
    test_edit_replace_all_is_denied
  * Edit whose old_string never matches,
    a true content no-op                 (v1: ALLOWED) -> now DENIED — v2's
    PROTECTED check is a pure path match, it never inspects tool_input at
    all, so "this edit wouldn't actually change anything" carries no
    weight whatsoever
    test_content_noop_edit_is_denied_too

Two guarantees the v1 file never had reason to state, because v1's single
global .claude/active-ticket had nothing analogous to "another session's
claim":

  * PROTECTED fires identically regardless of whether THIS session holds no
    claim, a claim on some other ticket, or another session entirely holds
    a claim — test_protected_regardless_of_whose_claim_is_active
  * the guarantee names BOTH .claude/claims/** and .claude/evidence/**, not
    only the one path this module's (legacy T-13) filename refers to —
    test_evidence_dir_is_equally_protected

IMPORTANT: run_hook's ``active_ticket`` parameter defaults to ``UNSET``
("leave the claim file as-is"); every test below relies on that default so
its own ``_seed()`` call survives into the hook invocation.
"""

from __future__ import annotations

from pathlib import Path

from .conftest import SCOPE_TEST_SESSION, decision, run_hook, write_claim

CLAIM_REL = ".claude/claims/probe-session.json"
EVIDENCE_REL = ".claude/evidence/T-1.json"


def _seed(project_dir: Path, rel: str, content: str) -> Path:
    path = project_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_pure_append_is_now_denied(project: Path) -> None:
    _seed(project, CLAIM_REL, '{"ticket":"T-1","session":"probe-session"}\n')
    result = run_hook(
        project,
        str(project / CLAIM_REL),
        tool_name="Write",
        write_content='{"ticket":"T-1","session":"probe-session"}\n{"ticket":"T-2"}\n',
    )
    assert decision(result) == "deny"


def test_full_rewrite_is_denied_and_file_left_untouched(project: Path) -> None:
    at_path = _seed(project, CLAIM_REL, '{"ticket":"T-5","session":"probe-session"}\n')
    before = at_path.read_text()

    result = run_hook(
        project, str(project / CLAIM_REL), tool_name="Write", write_content='{"ticket":"T-14"}\n',
    )
    assert decision(result) == "deny"
    # Unlike a hook that performed the write itself, this hook only ever
    # DECIDES — the prior claim record on disk survives untouched.
    assert at_path.read_text() == before


def test_first_ever_write_with_no_prior_file_is_denied(project: Path) -> None:
    result = run_hook(
        project, str(project / CLAIM_REL), tool_name="Write", write_content='{"ticket":"T-1"}\n',
    )
    assert decision(result) == "deny"


def test_edit_replacing_entire_content_is_denied(project: Path) -> None:
    _seed(project, CLAIM_REL, '{"ticket":"T-5"}\n')
    result = run_hook(
        project,
        str(project / CLAIM_REL),
        tool_name="Edit",
        old_string='{"ticket":"T-5"}\n',
        new_string='{"ticket":"T-14"}\n',
    )
    assert decision(result) == "deny"


def test_edit_modifying_one_earlier_line_is_denied(project: Path) -> None:
    """Not a full overwrite — a targeted in-place tamper of a SINGLE
    earlier line, with the rest of the (hypothetical) log left alone.
    Still denied: PROTECTED doesn't care what the edit would have done.
    """
    _seed(
        project,
        CLAIM_REL,
        '{"ticket":"T-1","session":"session-A"}\n{"ticket":"T-2","session":"session-B"}\n',
    )
    result = run_hook(
        project,
        str(project / CLAIM_REL),
        tool_name="Edit",
        old_string='{"ticket":"T-1","session":"session-A"}',
        new_string='{"ticket":"T-99","session":"session-A"}',
    )
    assert decision(result) == "deny"


def test_tail_anchored_append_edit_is_now_denied(project: Path) -> None:
    """The one v1 test whose own name promised an ALLOW — a legitimate way
    to append via the Edit tool by anchoring old_string on the tail of the
    current content. This is the whole point of this file's rewrite: v2
    denies it anyway. See module docstring.
    """
    _seed(project, CLAIM_REL, '{"ticket":"T-1"}\n')
    result = run_hook(
        project,
        str(project / CLAIM_REL),
        tool_name="Edit",
        old_string='{"ticket":"T-1"}\n',
        new_string='{"ticket":"T-1"}\n{"ticket":"T-2"}\n',
    )
    assert decision(result) == "deny"


def test_edit_replace_all_is_denied(project: Path) -> None:
    _seed(project, CLAIM_REL, '{"ticket":"T-1"}\n{"ticket":"T-1"}\n')
    result = run_hook(
        project,
        str(project / CLAIM_REL),
        tool_name="Edit",
        old_string='{"ticket":"T-1"}\n',
        new_string='{"ticket":"T-9"}\n',
        replace_all=True,
    )
    assert decision(result) == "deny"


def test_content_noop_edit_is_denied_too(project: Path) -> None:
    """v1's no-op-edit test existed to prove the append-only checker
    special-cased "old_string never matched" as harmless (a regression
    guard for T-12's own placeholder-Edit tests). v2 has no such special
    case: PROTECTED is a pure path match against tool_input.file_path, it
    never even looks at old_string/new_string/content, so a genuine no-op
    is denied exactly like every other write attempt in this file.
    """
    _seed(project, CLAIM_REL, '{"ticket":"T-5"}\n')
    result = run_hook(
        project, str(project / CLAIM_REL), tool_name="Edit", old_string="x", new_string="y",
    )
    assert decision(result) == "deny"


def test_protected_regardless_of_whose_claim_is_active(project: Path) -> None:
    """v1's single global active-ticket file had no concept of "whose"
    claim was active — there was only ever one. v2 claims are per-session
    files, so PROTECTED's unconditional deny needs to be shown NOT to
    depend on: no claim at all, a claim THIS session itself holds (on an
    unrelated ticket), or a claim a DIFFERENT session holds.
    """
    _seed(project, CLAIM_REL, '{"ticket":"T-5"}\n')
    path = str(project / CLAIM_REL)

    result_no_claim = run_hook(project, path, tool_name="Write", write_content="{}\n")
    assert decision(result_no_claim) == "deny"

    write_claim(project, "T-1", SCOPE_TEST_SESSION)
    result_own_claim = run_hook(project, path, tool_name="Write", write_content="{}\n")
    assert decision(result_own_claim) == "deny"

    write_claim(project, None, SCOPE_TEST_SESSION)
    write_claim(project, "T-1", "someone-elses-session")
    result_other_session_claim = run_hook(project, path, tool_name="Write", write_content="{}\n")
    assert decision(result_other_session_claim) == "deny"


def test_evidence_dir_is_equally_protected(project: Path) -> None:
    """The guarantee names .claude/evidence/** too, not only the claims
    dir this module's (legacy T-13) filename refers to.
    """
    _seed(project, EVIDENCE_REL, '{"ticket":"T-1","commit":"abc"}\n')
    result = run_hook(
        project,
        str(project / EVIDENCE_REL),
        tool_name="Write",
        write_content='{"ticket":"T-9"}\n',
    )
    assert decision(result) == "deny"
