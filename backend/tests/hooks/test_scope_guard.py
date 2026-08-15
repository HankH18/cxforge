"""T-12 acceptance: .claude/hooks/scope_guard.sh matches only intended paths.

Every test here drives the REAL hook script as a subprocess (see
conftest.run_hook / conftest.expect) — nothing in this file reimplements the
glob matcher. A synthetic CLAUDE_PROJECT_DIR (pytest's tmp_path, via the
``project`` fixture) stands in for the repo so no test ever reads or writes
the real repo's .claude/claims/ or .claude/evidence/.

T-31 (v2 harness migration) note, read before touching BASE_COVERAGE: v2's
harness_lib.PROTECTED denies Edit/Write to docs/tickets.json,
.claude/hooks/**, .claude/scripts/**, .claude/settings.json,
.claude/claims/** and .claude/evidence/** UNCONDITIONALLY — before any
per-ticket scope check runs at all, for every ticket, every session. Under
v1 several tickets (T-12, T-13, T-22, T-26, T-27, T-28, T-29, and now T-31)
declared one of those exact paths as part of their OWN scope, so v1's
BASE_COVERAGE used it as that ticket's "allow" probe. That probe would now
be denied for the wrong reason (PROTECTED, not scope) even while claimed,
so those seven-turned-eight rows below were moved to a DIFFERENT,
non-protected glob from that same ticket's scope — see
test_claude_hooks_and_scripts_are_never_writable_by_any_ticket below for
the guarantee that motivated the move, proven directly.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from .conftest import (
    REAL_SCOPES,
    REAL_TICKET_IDS,
    decision,
    expect,
    make_project,
    run_hook,
)

# ---------------------------------------------------------------------------
# Base coverage (acceptance 5): one allow (inside scope) + one near-miss deny
# (textually similar, but outside scope once the glob is anchored at both
# ends) per ticket, for EVERY ticket declared in docs/tickets.json today.
#
# Each (allow_path, deny_path, note) pair below was hand-verified against
# that ticket's *entire* scope list (not just the glob it's meant to probe)
# so the deny side can't accidentally match a different entry.
# ---------------------------------------------------------------------------
BASE_COVERAGE: list[tuple[str, str, str, str]] = [
    ("T-0", "backend/src/main.py", "docker-compose.prod.yml", "literal-file scope, not a prefix"),
    (
        "T-1", "backend/src/data/models.py", "backend/src/data_export/config.py",
        "'data' substring w/o '/' boundary",
    ),
    (
        "T-2", "backend/src/helpdesk/zendesk_adapter.py", "backend/src/helpdesk_v2/adapter.py",
        "helpdesk vs helpdesk_v2",
    ),
    (
        "T-3", "backend/src/helpdesk/email_adapter.py", "backend/src/helpdesk/zendesk_adapter.py",
        "exact single-file scope; sibling file belongs to T-2",
    ),
    (
        "T-4", "backend/src/ingress/webhook.py", "docs/zendesk-runbook-v2.md",
        "literal filename, not a prefix",
    ),
    ("T-5", "backend/src/agent/graph.py", "backend/src/agents/graph.py", "agent vs agents"),
    (
        "T-6", "backend/src/escalation/engine.py", "backend/src/escalations/engine.py",
        "escalation vs escalations",
    ),
    ("T-7", "evals/labeled_set.yaml", "eval/labeled_set.yaml", "missing trailing s"),
    ("T-8", "backend/src/portal/api.py", "portal/src/api.ts", "frontend, not T-8's backend scope"),
    (
        "T-9", "portal/src/App.tsx", "backend/src/portal/routes.py",
        "== ACCEPTANCE-1 regression pair, see test below",
    ),
    (
        "T-10", "backend/tests/live/test_e2e.py", "scripts/scenario_runner_v2.py",
        "literal filename, not a prefix",
    ),
    ("T-11", "docs/architecture.md", "deployment/config.yml", "deploy vs deployment"),
    (
        "T-12", "backend/tests/hooks/test_scope_guard.py",
        "backend/tests/hook/test_scope_guard.py",
        "tests/hooks vs tests/hook (probe moved off .claude/hooks/**, now PROTECTED)",
    ),
    (
        "T-13", "backend/tests/hooks/test_stop_guard.py", "backend/tests/hook/test_stop_guard.py",
        "tests/hooks vs tests/hook (probe moved off .claude/hooks/**, now PROTECTED)",
    ),
    (
        "T-14", "backend/tests/plan/test_invariants.py", "backend/tests/planning/test_x.py",
        "plan vs planning",
    ),
    ("T-15", "evals/report.py", "evals/reporting.py", "report.py vs reporting.py"),
    ("T-16", "backend/tests/graph/test_x.py", "backend/test/graph/test_x.py", "tests vs test"),
    (
        "T-17", "backend/tests/deploy/test_verify.py", "backend/tests/deployment/test_verify.py",
        "deploy vs deployment",
    ),
    (
        "T-18", "backend/src/escalation/classifier.py", "backend/src/escalations/classifier.py",
        "escalation vs escalations",
    ),
    (
        "T-19", "backend/src/portal/schemas.py", "backend/src/portals/schemas.py",
        "portal vs portals",
    ),
    ("T-20", "deploy/backend/Dockerfile", "deploy/backends/Dockerfile", "backend vs backends"),
    (
        "T-21", "docs/eval-report/metrics.json", "docs/eval-reports/metrics.json",
        "eval-report vs eval-reports",
    ),
    (
        "T-22", "backend/tests/hooks/test_verify_gate.py",
        "backend/tests/hooked/test_status_sync.py",
        "hooks vs hooked (probe moved off .claude/hooks/**, now PROTECTED)",
    ),
    ("T-23", "backend/tests/conftest.py", "backend/test_utils/conftest.py", "tests vs test_utils"),
    ("T-24", "backend/src/data/db.py", "backend/src/database/db.py", "data vs database"),
    (
        "T-25", "evals/report.py", "evals/report_utils.py",
        "report.py vs report_utils.py",
    ),
    (
        "T-26", "docs/INGEST.md", "docs/INGEST.md.bak",
        "literal filename, not a prefix (probe moved off docs/tickets.json, now PROTECTED)",
    ),
    (
        "T-27", "backend/tests/hooks/test_settings_guard.py",
        "backend/tests/hook/test_settings_guard.py",
        "tests/hooks vs tests/hook (probe moved off .claude/settings.json, now PROTECTED)",
    ),
    (
        "T-28", "backend/tests/hooks/test_stop_guard_v2.py",
        "backend/tests/hook/test_stop_guard_v2.py",
        "tests/hooks vs tests/hook (probe moved off .claude/hooks/**, now PROTECTED)",
    ),
    (
        "T-29", "backend/tests/hooks/test_claim_lookup.py",
        "backend/tests/hook/test_claim_lookup.py",
        "tests/hooks vs tests/hook (probe moved off .claude/hooks/**, now PROTECTED)",
    ),
    (
        "T-30", "backend/src/escalation/rules.py", "backend/src/escalation/engine.py",
        "single-file scope; sibling file belongs to other tickets",
    ),
    (
        "T-31", "backend/tests/hooks/test_harness_lib_conftest.py",
        "backend/tests/hook/test_harness_lib_conftest.py",
        "tests/hooks vs tests/hook (T-31's other scope globs are all PROTECTED too)",
    ),
]


def test_base_coverage_spans_every_real_ticket() -> None:
    """The table above must track docs/tickets.json, not a stale snapshot.

    Read-only cross-check against the real repo file: if a ticket is added,
    removed or renamed there, this fails until BASE_COVERAGE is updated —
    it can never silently under-cover the plan.
    """
    assert {row[0] for row in BASE_COVERAGE} == set(REAL_TICKET_IDS)


_BASE_IDS = [row[0] for row in BASE_COVERAGE]
_BASE_FIELDS = ("ticket", "allow_path", "deny_path", "note")


@pytest.mark.parametrize(_BASE_FIELDS, BASE_COVERAGE, ids=_BASE_IDS)
def test_base_coverage_allow(
    project: Path, ticket: str, allow_path: str, deny_path: str, note: str,
) -> None:
    del deny_path, note
    expect(project, ticket=ticket, file_path=str(project / allow_path), want="allow")


@pytest.mark.parametrize(_BASE_FIELDS, BASE_COVERAGE, ids=_BASE_IDS)
def test_base_coverage_deny(
    project: Path, ticket: str, allow_path: str, deny_path: str, note: str,
) -> None:
    del allow_path, note
    expect(project, ticket=ticket, file_path=str(project / deny_path), want="deny")


_WRITE_SAMPLE = BASE_COVERAGE[:6]
_WRITE_ALLOW_PARAMS = [(row[0], row[1]) for row in _WRITE_SAMPLE]
_WRITE_DENY_PARAMS = [(row[0], row[2]) for row in _WRITE_SAMPLE]
_WRITE_PARAMS = _WRITE_ALLOW_PARAMS + _WRITE_DENY_PARAMS
_WRITE_ALLOW_IDS = [f"{r[0]}-allow-write" for r in _WRITE_SAMPLE]
_WRITE_DENY_IDS = [f"{r[0]}-deny-write" for r in _WRITE_SAMPLE]
_WRITE_IDS = _WRITE_ALLOW_IDS + _WRITE_DENY_IDS


@pytest.mark.parametrize(("ticket", "path"), _WRITE_PARAMS, ids=_WRITE_IDS)
def test_write_tool_agrees_with_edit_tool(project: Path, ticket: str, path: str) -> None:
    """The Write tool must reach the identical decision as Edit for the same path.

    Exercised over a representative subset (not the full 22-ticket table) to
    keep runtime reasonable — the hook's logic never branches on tool_name,
    so if these agree the rest do too.
    """
    edit_result = run_hook(project, str(project / path), tool_name="Edit", active_ticket=ticket)
    write_result = run_hook(project, str(project / path), tool_name="Write", active_ticket=ticket)
    assert decision(edit_result) == decision(write_result)


# ---------------------------------------------------------------------------
# Acceptance 1, verbatim: with scope "portal/**" the path
# "backend/src/portal/routes.py" is DENIED. T-9's scope is exactly
# ["portal/**"] — the real ticket that declares it, isolated from every
# other glob so there's no ambiguity about which entry is under test.
# ---------------------------------------------------------------------------
def test_acceptance_1_portal_glob_does_not_prefix_match_nested_portal_dir(project: Path) -> None:
    assert REAL_SCOPES["T-9"] == ["portal/**"]
    expect(
        project,
        ticket="T-9",
        file_path=str(project / "backend/src/portal/routes.py"),
        want="deny",
    )


# ---------------------------------------------------------------------------
# Acceptance 1, end-anchor half: a literal (non-"**"-suffixed) scope entry
# must not match as a PREFIX of a longer path. The "portal/**" regression
# above cannot exercise this: a "**"-suffixed pattern's own ".*" already
# consumes anything past the pattern under Python's `.match()` regardless of
# whether a trailing "$" is present, so dropping that "$" is invisible to
# any test built only from "**" scopes. It is NOT invisible here: without
# the trailing "$", `.match()` only anchors at the START of the string, so
# a literal pattern like "docker-compose.yml" would still match
# "docker-compose.ymlEXTRA". Each (ticket, literal_entry) pair below was
# hand cross-checked against that ticket's ENTIRE scope list (see
# test_literal_probes_are_cross_checked_against_real_tickets) to confirm no
# *other* glob in the ticket's scope legitimately covers the probe path, so
# a false ALLOW here can only come from the end anchor being broken.
# ---------------------------------------------------------------------------
LITERAL_SUFFIX_PROBES: list[tuple[str, str]] = [
    ("T-0", "docker-compose.yml"),
    ("T-2", "scripts/live_smoke.py"),
    ("T-3", "backend/src/helpdesk/email_adapter.py"),
    ("T-4", "docs/zendesk-runbook.md"),
    ("T-10", "scripts/scenario_runner.py"),
    ("T-11", "scripts/verify_deploy.sh"),
    ("T-14", "scripts/render_tasks_md.py"),
    ("T-15", "evals/report.py"),
    ("T-16", "docker-compose.yml"),
    ("T-17", "scripts/verify_deploy.sh"),
    ("T-21", "evals/report.py"),
]


def test_literal_probes_are_cross_checked_against_real_tickets() -> None:
    """The table above must track docs/tickets.json, not a hand-typed guess:
    every literal_entry must appear verbatim in that ticket's real scope,
    and must genuinely contain no wildcard (otherwise it isn't testing the
    no-wildcard-tail case this block exists for).
    """
    for ticket, entry in LITERAL_SUFFIX_PROBES:
        assert entry in REAL_SCOPES[ticket], (ticket, entry)
        assert "*" not in entry, f"{entry!r} is not a literal scope entry"


@pytest.mark.parametrize(
    ("ticket", "entry"),
    LITERAL_SUFFIX_PROBES,
    ids=[t for t, _ in LITERAL_SUFFIX_PROBES],
)
def test_acceptance_1_literal_scope_does_not_match_as_a_prefix_of_a_longer_path(
    project: Path, ticket: str, entry: str,
) -> None:
    """Mutating away the trailing "$" in scope_guard.sh's
    ``re.compile("^(?:" + pattern + ")$")`` leaves this red: without it, a
    literal scope entry would still match any path it is merely a PREFIX
    of, since Python's `.match()` only anchors matching at the start.
    """
    probe = str(project / (entry + "-EXTRA-SUFFIX-not-in-scope"))
    expect(project, ticket=ticket, file_path=probe, want="deny")


def test_same_path_allowed_under_a_ticket_whose_scope_actually_lists_it(project: Path) -> None:
    """Same literal path as the regression above, different ticket.

    T-19 and T-8 both explicitly declare backend/src/portal/** — proves the
    matcher is genuinely per-ticket-scope, not a hardcoded ban on that path.
    """
    assert "backend/src/portal/**" in REAL_SCOPES["T-19"]
    assert "backend/src/portal/**" in REAL_SCOPES["T-8"]
    path = str(project / "backend/src/portal/routes.py")
    expect(project, ticket="T-19", file_path=path, want="allow")
    expect(project, ticket="T-8", file_path=path, want="allow")


# ---------------------------------------------------------------------------
# Realpath / traversal / out-of-repo (acceptance 2)
# ---------------------------------------------------------------------------
def test_traversal_that_resolves_into_scope_is_allowed(project: Path) -> None:
    """'..' cancels out to an in-scope path — the guard follows realpath, not text."""
    path = str(project / "backend/src/agent/../data/models.py")
    expect(project, ticket="T-1", file_path=path, want="allow")


def test_traversal_that_resolves_back_in_repo_but_out_of_scope_is_denied(project: Path) -> None:
    """'..' segments cancel exactly, landing back inside the repo — at a path
    the active ticket's scope does not cover. Traversal must not get a free
    pass just because '..' was involved: outside/x.py is still outside T-5.
    """
    path = str(project / "backend/src/agent/../../../outside/x.py")
    # Sanity: this really does resolve back inside the project root.
    assert os.path.realpath(path) == str(project / "outside" / "x.py")
    expect(project, ticket="T-5", file_path=path, want="deny")


def test_traversal_that_escapes_the_repo_entirely_is_ignored(project: Path) -> None:
    """Enough '..' segments to leave CLAUDE_PROJECT_DIR altogether -> exit 0,
    silently, before any scope (or even claim) check — acceptance 2.
    """
    path = str(project) + ("/.." * 8) + "/outside-the-repo.py"
    assert not os.path.realpath(path).startswith(str(project) + os.sep)
    expect(project, ticket="T-5", file_path=path, want="allow")


def test_out_of_repo_absolute_path_is_ignored(project: Path) -> None:
    """A path that never shared CLAUDE_PROJECT_DIR as a prefix at all."""
    expect(project, ticket="T-5", file_path="/etc/hosts", want="allow")


def test_out_of_repo_path_is_ignored_even_with_no_active_ticket(project: Path) -> None:
    """Acceptance 2's exit-0 happens BEFORE the claim record is even read —
    an out-of-repo path is allowed regardless of claim state.
    """
    expect(project, ticket=None, file_path="/etc/hosts", want="allow")


# ---------------------------------------------------------------------------
# Claim-state behaviour. v1 called this section "fail-closed claim record"
# (acceptance 3): v1's guard denied EVERY Edit/Write whenever the session
# had no active-ticket file at all — deny was the default posture absent
# any claim.
#
# v2 FLIPS that default. harness_lib.py's guard(), decision-order step 5,
# is explicit about it in the source: "session holds no claim -> allow"
# (comment: "non-owning sessions are unconstrained (jarvis T-21)"). The
# scope check exists to keep a CLAIM-HOLDING session inside its own
# ticket's declared scope, not to gate all activity behind holding a claim
# in the first place. This is documented, authoritative behaviour in
# harness_lib.py — out of scope for T-31 to second-guess — not a bug this
# file may paper over by asserting the old outcome anyway.
#
# What still fails closed, UNCHANGED from v1: a session that DOES hold a
# claim, naming a ticket id with no entry in docs/tickets.json, still can't
# write anywhere outside PROTECTED/META_ALLOW.
# test_empty_active_ticket_denies / test_whitespace_only_active_ticket_denies
# / test_unknown_ticket_id_denies below are UNCHANGED from v1 in both call
# and outcome (all three still deny) — the mechanism moved (all three now
# exercise the identical harness_lib.ticket(id) -> None fallback, rather
# than three distinct free-text parse failures) but the guarantee didn't.
# ---------------------------------------------------------------------------
def test_no_claim_at_all_is_unconstrained_outside_protected_paths(project: Path) -> None:
    """v1: test_missing_active_ticket_denies, asserted "deny". Superseded by
    the v2 flip described in this section's docstring above — renamed so
    the function name doesn't keep asserting something false.
    """
    data_path = str(project / "backend/src/data/models.py")
    expect(project, ticket=None, file_path=data_path, want="allow")


def test_empty_active_ticket_denies(project: Path) -> None:
    data_path = str(project / "backend/src/data/models.py")
    expect(project, ticket="", file_path=data_path, want="deny")


def test_whitespace_only_active_ticket_denies(project: Path) -> None:
    data_path = str(project / "backend/src/data/models.py")
    expect(project, ticket="   \n", file_path=data_path, want="deny")


def test_unknown_ticket_id_denies(project: Path) -> None:
    """T-99 is not in docs/tickets.json: a claim naming it still can't reach
    anywhere outside PROTECTED/META_ALLOW.
    """
    assert "T-99" not in REAL_TICKET_IDS
    data_path = str(project / "backend/src/data/models.py")
    expect(project, ticket="T-99", file_path=data_path, want="deny")


def test_bypass_env_vars_never_turn_a_deny_into_an_allow(project: Path) -> None:
    """Negative control: a self-issued env var must not stand in for a claim.

    v1's version of this test used a no-claim scenario, which ALSO denied
    under v1 — but that exact scenario now ALLOWS under v2 (see
    test_no_claim_at_all_is_unconstrained_outside_protected_paths above),
    so it stopped demonstrating anything about env vars specifically.
    Re-pointed at a scenario v2 DOES still deny — a real claim, on a path
    outside that ticket's own declared scope — to preserve the original
    point: harness_lib.py's guard() reads no bypass env var of any kind,
    ever. These flags are just as inert here as everywhere else.
    """
    result = run_hook(
        project,
        str(project / "backend/src/agents/graph.py"),  # outside T-5: 'agent' vs 'agents'
        active_ticket="T-5",
        env_extra={
            "SCOPE_GUARD_BYPASS": "1",
            "CLAUDE_SCOPE_GUARD_SKIP": "1",
            "SCOPE_GUARD_ALLOW_ALL": "true",
        },
    )
    assert decision(result) == "deny"


# ---------------------------------------------------------------------------
# Protocol paths (acceptance 4 + 7)
# ---------------------------------------------------------------------------
def test_claim_file_itself_is_never_writable_via_edit_or_write(project: Path) -> None:
    """v1: .claude/active-ticket WAS how a claim got made — an agent wrote
    it directly with the Edit/Write tool, so acceptance 4/7 required it
    stay writable (see the two tests this replaces:
    test_claim_record_itself_stays_writable /
    test_claim_record_writable_even_with_no_prior_claim, both asserted
    "allow"). v2 flips this completely: a claim is made exclusively by
    running `claim.sh claim <tid> <note>` (harness_lib.py's cmd_claim
    writes .claude/claims/<session>.json directly from Python — never
    through the Edit/Write tool), and that whole directory is listed in
    harness_lib.PROTECTED, so the Edit/Write path to it is now DENIED,
    unconditionally, for every session — with or without a claim.
    Exhaustively covered (append/rewrite/no-op/whose-claim variations) in
    test_scope_guard_append_only.py; this is a one-line regression pin so
    this file's own "Protocol paths" section still states the guarantee.
    """
    expect(
        project,
        ticket="T-5",
        file_path=str(project / ".claude/claims/some-other-session.json"),
        want="deny",
    )
    expect(
        project,
        ticket=None,
        file_path=str(project / ".claude/claims/some-other-session.json"),
        want="deny",
    )


@pytest.mark.parametrize("ticket", REAL_TICKET_IDS, ids=REAL_TICKET_IDS)
def test_claude_hooks_and_scripts_are_never_writable_by_any_ticket(
    project: Path, ticket: str,
) -> None:
    """v2-only guarantee, with no v1 analogue: T-12/T-13/T-22/T-27/T-28/T-29
    (and now T-31) used to declare .claude/hooks/** as part of their OWN
    v1 scope specifically so the ticket implementing them could edit those
    files — that's why v1's BASE_COVERAGE used exactly that path as their
    "allow" probe. v2 removes that entirely: .claude/hooks/** and
    .claude/scripts/** are harness_lib.PROTECTED unconditionally, so even
    THOSE tickets can no longer Edit/Write there; only claim.sh (invoked
    directly, never through the Edit/Write tool) may touch harness state,
    and any other change is a plan defect for .claude/NEEDS_HUMAN.md, not a
    ticket scope. This is the guarantee that forced BASE_COVERAGE's T-12 /
    T-13 / T-22 / T-26 / T-27 / T-28 / T-29 / T-31 rows to move their
    "allow" probe onto a different, non-protected glob — proven directly,
    for every real ticket, here.
    """
    expect(
        project,
        ticket=ticket,
        file_path=str(project / ".claude/hooks/scope_guard.sh"),
        want="deny",
    )
    expect(
        project,
        ticket=ticket,
        file_path=str(project / ".claude/scripts/harness_lib.py"),
        want="deny",
    )


@pytest.mark.parametrize("ticket", REAL_TICKET_IDS, ids=REAL_TICKET_IDS)
def test_evidence_dir_is_never_writable_by_any_ticket(project: Path, ticket: str) -> None:
    """Acceptance 7, unconditional: no ticket's scope may ever permit a write
    under .claude/evidence/**. Outcome unchanged from v1, but the mechanism
    is now structural rather than incidental: v1 denied this only because
    no real ticket's scope happened to list it; v2 denies it via
    harness_lib.PROTECTED regardless of what any ticket's scope says (step
    3, ahead of any per-ticket scope check) — evidence is cmd_close's alone
    to write.
    """
    expect(
        project,
        ticket=ticket,
        file_path=str(project / f".claude/evidence/{ticket}.pass"),
        want="deny",
    )


def test_evidence_dir_denies_writing_another_tickets_evidence_too(project: Path) -> None:
    expect(
        project,
        ticket="T-15",
        file_path=str(project / ".claude/evidence/T-21.pass"),
        want="deny",
    )


def test_evidence_dir_denies_arbitrary_filenames_under_it(project: Path) -> None:
    expect(
        project,
        ticket="T-12",
        file_path=str(project / ".claude/evidence/whatever-i-name-it.txt"),
        want="deny",
    )


# ---------------------------------------------------------------------------
# Adversarial: the old blanket '.claude/*|docs/*' allow-everything branch is
# gone. A ticket with no docs/** or .claude/** entry in its own scope must
# be denied there, exactly like anywhere else (acceptance 4).
# ---------------------------------------------------------------------------
def test_docs_tree_is_not_blanket_allowed_for_a_ticket_without_it_in_scope(project: Path) -> None:
    assert not any(g.startswith("docs/") or g == "docs/**" for g in REAL_SCOPES["T-1"])
    expect(project, ticket="T-1", file_path=str(project / "docs/architecture.md"), want="deny")


def test_docs_tree_is_not_blanket_allowed_second_ticket(project: Path) -> None:
    assert not any(g.startswith("docs/") or g == "docs/**" for g in REAL_SCOPES["T-5"])
    expect(project, ticket="T-5", file_path=str(project / "docs/agent-design.md"), want="deny")


def test_claude_tree_is_not_blanket_allowed_for_a_ticket_without_it_in_scope(project: Path) -> None:
    """Probes a .claude/** path that is neither PROTECTED nor META_ALLOW
    (unlike .claude/hooks/scope_guard.sh, which — see
    test_claude_hooks_and_scripts_are_never_writable_by_any_ticket above —
    is now denied for EVERY ticket regardless of scope, so it would no
    longer isolate "not in this ticket's scope" as the reason for the
    deny). This one genuinely reaches the per-ticket scope check.
    """
    assert not any(g.startswith(".claude/") for g in REAL_SCOPES["T-1"])
    expect(
        project,
        ticket="T-1",
        file_path=str(project / ".claude/some-random-state.json"),
        want="deny",
    )


# ---------------------------------------------------------------------------
# Glob semantics: '*' within one segment only, '**' crosses segments.
# ---------------------------------------------------------------------------
def test_double_star_crosses_multiple_nested_subdirectories(project: Path) -> None:
    """T-1's 'backend/src/data/**' must reach arbitrarily deep nesting —
    proves '**' really is the segment-crossing form. (This does NOT exercise
    single-'*' semantics — see test_single_star_does_not_cross_a_path_separator
    below for that, which needs a synthetic scope since no real ticket
    declares a bare single '*'.)
    """
    expect(
        project,
        ticket="T-1",
        file_path=str(project / "backend/src/data/nested/deep/models.py"),
        want="allow",
    )


def test_single_star_does_not_cross_a_path_separator(tmp_path: Path) -> None:
    """A bare single '*' — no real ticket in docs/tickets.json declares one
    today (every wildcard scope entry in the real plan uses '**'), so this
    builds a synthetic ticket via ``make_project`` to exercise the single-'*'
    branch directly rather than asserting something '**'-shaped and calling
    it done.
    """
    synthetic = make_project(
        tmp_path,
        {"tickets": [{"id": "T-STAR", "scope": ["backend/src/data/*/models.py"]}]},
    )
    # Exactly one path segment between "data/" and "models.py": within a
    # single '*''s reach.
    expect(
        synthetic,
        ticket="T-STAR",
        file_path=str(synthetic / "backend/src/data/nested/models.py"),
        want="allow",
    )
    # Two segments ("nested/deep") between "data/" and "models.py": a single
    # '*' must NOT cross the extra '/' to reach it.
    expect(
        synthetic,
        ticket="T-STAR",
        file_path=str(synthetic / "backend/src/data/nested/deep/models.py"),
        want="deny",
    )


def test_exact_literal_scope_matches_nothing_but_itself(project: Path) -> None:
    """T-15's 'evals/report.py' has no wildcard: it must match that path and
    only that path, not a same-named file elsewhere in the tree.
    """
    assert "evals/report.py" in REAL_SCOPES["T-15"]
    expect(project, ticket="T-15", file_path=str(project / "evals/report.py"), want="allow")
    expect(
        project,
        ticket="T-15",
        file_path=str(project / "backend/evals/report.py"),
        want="deny",
    )
