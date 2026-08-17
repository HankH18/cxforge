"""Regression tests for two blast-radius-detection shapes an adversarial
review found undetected in T-14's first pass (see the T-14 repair handoff).
Both were fixed in ``_planlib.py``; these tests exist so neither can quietly
come back.

1. ``scope_touched_packages`` (and its siblings ``scope_owned_test_dirs``,
   ``scope_needs_npm_portal``) used to decide "does this ticket's scope
   touch package/dir X" by probing a single hard-coded literal path (e.g.
   "backend/src/<pkg>/__init__.py") against the scope's globs -- silently
   blind to a scope entry naming ONE specific file inside that package
   instead of a "**" wildcard. T-3's real scope is exactly that shape:
   ``["backend/src/helpdesk/email_adapter.py", "backend/tests/contract/**",
   "README.md"]`` -- no "__init__.py" in sight -- so the old probe reported
   "helpdesk not touched" and T-3's verify sailed through acceptance 1
   while genuinely missing graph/grounding/ingress/portal/evals/escalation,
   every one of which imports ``helpdesk.email_adapter`` via a conftest.
   Fixed by ``_glob_reaches_prefix``'s segment-wise glob/prefix overlap
   check, which needs no representative file to exist on disk.

2. ``find_self_gating_violations`` matched a candidate token against a
   ticket's own scope globs via an EXACT string comparison
   (``path_matches_any_glob``, anchored both ends). A self-authored script
   invoked as ``./scripts/verify_deploy.sh`` -- an entirely ordinary shell
   idiom, not an exotic evasion -- does not string-equal the scope entry
   ``"scripts/verify_deploy.sh"``, so it slipped through undetected: the
   exact T-11 / verify_deploy.sh self-gating shape acceptance 2 exists to
   forbid, reachable by adding one "./" . Fixed by normalizing candidate
   tokens with ``_normalize_repo_relative`` (posixpath.normpath, stripping
   a residual leading "./") before matching.
"""

from __future__ import annotations

from ._planlib import (
    build_import_graph,
    find_self_gating_violations,
    load_tickets,
    required_test_dirs_for_ticket,
    scope_needs_npm_portal,
    scope_owned_test_dirs,
    scope_touched_packages,
)

# --------------------------------------------------------------------------
# 1. single-file (non-wildcard) scope entries must still be detected
# --------------------------------------------------------------------------


def test_single_file_scope_entry_inside_a_package_is_detected() -> None:
    """T-3's real shape: a scope entry naming ONE file inside an existing
    package, not a "backend/src/<pkg>/**" wildcard, must still register as
    touching that package."""
    scope = ["backend/src/helpdesk/email_adapter.py", "backend/tests/contract/**", "README.md"]
    assert scope_touched_packages(scope) == {"helpdesk"}


def test_single_file_scope_entry_inside_a_test_dir_is_detected() -> None:
    scope = ["backend/tests/contract/test_port_contract.py"]
    assert scope_owned_test_dirs(scope) == {"contract"}


def test_single_file_scope_entry_inside_portal_frontend_is_detected() -> None:
    scope = ["portal/src/api.ts"]  # neither "portal/**" nor the probed "App.tsx"
    assert scope_needs_npm_portal(scope) is True


def test_backend_src_portal_python_package_does_not_false_positive_npm() -> None:
    """Sibling sanity check: backend/src/portal/** (the Python package,
    T-8's scope) must NOT be mistaken for the portal/** frontend tree --
    same trailing directory name, different repo root."""
    assert scope_needs_npm_portal(["backend/src/portal/**"]) is False


def test_t3_shaped_scope_pulls_in_its_real_reverse_dependencies() -> None:
    """Live check against the actual current import graph: a single-file
    scope entry inside backend/src/helpdesk/ must resolve to helpdesk's
    real reverse-dependency set, which reaches well beyond "contract" today
    -- exactly what the unfixed probe-based check silently missed."""
    graph = build_import_graph()
    fake_ticket = {
        "scope": ["backend/src/helpdesk/email_adapter.py", "backend/tests/contract/**"],
    }
    required = required_test_dirs_for_ticket(fake_ticket, graph)
    assert "contract" in required  # its own declared suite
    assert required - {"contract"}, (
        "expected backend/src/helpdesk/email_adapter.py to have at least one "
        "real reverse-dependency suite beyond its own 'contract' suite -- if "
        "this now legitimately fails, helpdesk.email_adapter is no longer "
        "imported anywhere else and this assertion should be revisited"
    )


# --------------------------------------------------------------------------
# 2. a "./"-prefixed (or otherwise unnormalized) self-authored script must
#    still be caught as self-gating
# --------------------------------------------------------------------------


def test_dot_slash_prefixed_self_authored_script_is_still_caught() -> None:
    """T-11 / verify_deploy.sh shape, spelled with a leading "./". Uses
    T-11's REAL scope (which genuinely lists scripts/verify_deploy.sh) so
    this tracks the live ticket rather than a synthetic stand-in that could
    drift from it."""
    tickets = {t["id"]: t for t in load_tickets()}
    t11_scope = tickets["T-11"]["scope"]
    assert "scripts/verify_deploy.sh" in t11_scope  # guard: still true today

    sabotaged = {
        "scope": t11_scope,
        "verify": "bash ./scripts/verify_deploy.sh && uv run pytest backend/tests/hooks -q",
    }
    assert find_self_gating_violations(sabotaged) == ["./scripts/verify_deploy.sh"]


def test_bare_self_authored_script_is_still_caught() -> None:
    """Regression guard that the normalization fix didn't disturb the
    already-working un-prefixed case."""
    sabotaged = {
        "scope": ["scripts/verify_deploy.sh"],
        "verify": "bash scripts/verify_deploy.sh && uv run pytest backend/tests/hooks -q",
    }
    assert find_self_gating_violations(sabotaged) == ["scripts/verify_deploy.sh"]


def test_doubled_slash_and_dot_dot_spellings_are_also_normalized() -> None:
    """Same normalization class as the "./" case -- doubled separators and
    an up-and-back-down traversal must not reopen the hole either."""
    scope = ["scripts/verify_deploy.sh"]
    assert find_self_gating_violations(
        {"scope": scope, "verify": "bash scripts//verify_deploy.sh"}
    ) == ["scripts//verify_deploy.sh"]
    assert find_self_gating_violations(
        {"scope": scope, "verify": "bash scripts/../scripts/verify_deploy.sh"}
    ) == ["scripts/../scripts/verify_deploy.sh"]


def test_dot_slash_directory_argument_to_pytest_remains_exempt() -> None:
    """The self-gating fix must not accidentally start flagging the
    legitimate, exempt TDD shape (a ticket authoring the SUITE that gates
    it) merely because someone spells the directory argument with a
    leading "./" ."""
    owns_suite = {
        "scope": ["backend/tests/plan/**"],
        "verify": "uv run pytest ./backend/tests/plan -q",
    }
    assert find_self_gating_violations(owns_suite) == []
