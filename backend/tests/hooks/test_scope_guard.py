"""T-12 acceptance: .claude/hooks/scope_guard.sh matches only intended paths.

Every test here drives the REAL hook script as a subprocess (see
conftest.run_hook / conftest.expect) — nothing in this file reimplements the
glob matcher. A synthetic CLAUDE_PROJECT_DIR (pytest's tmp_path, via the
``project`` fixture) stands in for the repo so no test ever reads or writes
the real .claude/active-ticket.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from .conftest import (
    REAL_SCOPES,
    REAL_TICKET_IDS,
    UNSET,
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
        "T-12", ".claude/hooks/scope_guard.sh", ".claude/hooks_backup/scope_guard.sh",
        "hooks vs hooks_backup",
    ),
    ("T-13", ".claude/hooks/stop_guard.sh", ".claude/hook/stop_guard.sh", "missing s"),
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
        "T-22", ".claude/hooks/verify_gate.sh", "backend/tests/hooked/test_status_sync.py",
        "hooks vs hooked",
    ),
    ("T-23", "backend/tests/conftest.py", "backend/test_utils/conftest.py", "tests vs test_utils"),
    ("T-24", "backend/src/data/db.py", "backend/src/database/db.py", "data vs database"),
    (
        "T-25", "evals/report.py", "evals/report_utils.py",
        "report.py vs report_utils.py",
    ),
    (
        "T-26", "docs/tickets.json", "docs/tickets.json.bak",
        "literal filename, not a prefix",
    ),
    (
        "T-27", ".claude/settings.json", ".claude/settings.local.json",
        "settings.json vs settings.local.json",
    ),
    (
        "T-28", ".claude/hooks/stop_guard.sh", ".claude/stop_guard.sh",
        "hooks subdir, not .claude root",
    ),
    (
        "T-29", ".claude/hooks/claim_lookup.py", ".claude/hooklib/claim_lookup.py",
        "hooks vs hooklib",
    ),
    (
        "T-30", "backend/src/escalation/rules.py", "backend/src/escalation/engine.py",
        "single-file scope; sibling file belongs to other tickets",
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
# Fail-closed claim record (acceptance 3) — no bypass of any kind
# ---------------------------------------------------------------------------
def test_missing_active_ticket_denies(project: Path) -> None:
    data_path = str(project / "backend/src/data/models.py")
    expect(project, ticket=None, file_path=data_path, want="deny")


def test_empty_active_ticket_denies(project: Path) -> None:
    data_path = str(project / "backend/src/data/models.py")
    expect(project, ticket="", file_path=data_path, want="deny")


def test_whitespace_only_active_ticket_denies(project: Path) -> None:
    data_path = str(project / "backend/src/data/models.py")
    expect(project, ticket="   \n", file_path=data_path, want="deny")


def test_unknown_ticket_id_denies(project: Path) -> None:
    """T-99 is not in docs/tickets.json: undefined scope must fail closed."""
    assert "T-99" not in REAL_TICKET_IDS
    data_path = str(project / "backend/src/data/models.py")
    expect(project, ticket="T-99", file_path=data_path, want="deny")


def test_no_env_var_substitutes_for_a_real_claim(project: Path) -> None:
    """Negative control: a self-issued env var must not stand in for a claim.

    Even with no active-ticket file, an agent cannot wave a flag at the
    guard to make it allow anyway.
    """
    (project / ".claude" / "active-ticket").unlink(missing_ok=True)
    result = run_hook(
        project,
        str(project / "backend/src/data/models.py"),
        active_ticket=UNSET,
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
def test_claim_record_itself_stays_writable(project: Path) -> None:
    """.claude/active-ticket is how a claim is made — the narrowed
    replacement for the old blanket '.claude/*' branch.
    """
    expect(
        project,
        ticket="T-5",
        file_path=str(project / ".claude/active-ticket"),
        want="allow",
    )


def test_claim_record_writable_even_with_no_prior_claim(project: Path) -> None:
    """A brand new session with nothing claimed yet must still be able to
    write .claude/active-ticket for the very first time.
    """
    expect(project, ticket=None, file_path=str(project / ".claude/active-ticket"), want="allow")


@pytest.mark.parametrize("ticket", REAL_TICKET_IDS, ids=REAL_TICKET_IDS)
def test_evidence_dir_is_never_writable_by_any_ticket(project: Path, ticket: str) -> None:
    """Acceptance 7, unconditional: no ticket's scope may ever permit a write
    under .claude/evidence/**, including T-12/T-13 whose own scope is
    .claude/hooks/** — evidence is verify_gate.sh's alone to write.
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
    assert not any(g.startswith(".claude/") for g in REAL_SCOPES["T-1"])
    expect(
        project,
        ticket="T-1",
        file_path=str(project / ".claude/hooks/scope_guard.sh"),
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
