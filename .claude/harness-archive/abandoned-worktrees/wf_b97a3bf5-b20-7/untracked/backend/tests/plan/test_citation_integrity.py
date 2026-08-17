"""Citation and attribution integrity (W7, W13/W18).

Three defects, observed in this repo, that share one shape: a claim that
looks sourced and is not.

  W7a  ``T31-brief.md`` is cited five times across two files in
       backend/tests/hooks/. No commit in this repository has ever
       contained a file by that name.
  W7b  test_close_unattributed_claim_gap.py justifies shipping a test file
       that records its own acceptance as unmet by quoting "T-28's own
       escape valve". T-28's entry in docs/tickets.json contains no such
       text, and none of the words 'escape', 'valve', 'unreachable',
       'plainly' or 'inventing'.
  W13/ Commit 61d26de, subject "chore: regenerate docs/TASKS.md", changed
  W18  100 lines of .claude/scripts/harness_lib.py -- the file that decides
       what any session may write -- and never mentioned it.

None of the three is detectable by reading the artifact alone: each is
false only relative to something else (the filesystem, the ticket, the
diff). That is what makes them worth automating.

WHAT THESE TESTS RUN AGAINST
    The three ``test_no_new_*`` tests below read the REAL working tree and
    the REAL git history. The synthetic cases at the bottom are proof that
    the detectors still detect -- they are additional evidence, never a
    substitute, and ``test_the_real_scan_is_not_vacuous`` exists so that a
    detector which silently stops finding anything (a wrong root, an empty
    file list) fails loudly instead of passing green.

    That failure mode is not hypothetical: while this module was being
    written, ``iter_prose_files`` matched its skip-list against absolute
    path components, so running from an agent worktree under
    ``.claude/worktrees/`` skipped every file in the repo and all three
    checks reported zero findings.

THE BASELINE
    known_citation_defects.json registers the defects that exist today and
    have not been repaired. It is enforced in both directions: an
    unregistered finding fails, and a registered finding that no longer
    reproduces also fails, so the register cannot decay into a suppression
    list. Every entry carries its evidence and the repair it awaits.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from . import _citationlib as C

Baseline = dict[tuple[str, str, str], dict]

BASELINE_PATH = Path(__file__).parent / "known_citation_defects.json"


def _baseline() -> Baseline:
    raw = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    return {(e["check"], e["where"], e["subject"]): e for e in raw["findings"]}


def _report(findings: list[C.Finding]) -> str:
    return "\n\n".join(str(f) for f in findings)


# --------------------------------------------------------------------------
# The real repo
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def baseline() -> Baseline:
    return _baseline()


def _unregistered(findings: list[C.Finding], baseline: Baseline) -> list[C.Finding]:
    return [f for f in findings if f.key() not in baseline]


def test_no_new_dangling_md_citations(baseline: Baseline) -> None:
    """Every ``*.md`` filename in a docstring or comment names a document
    that exists."""
    new = _unregistered(C.find_dangling_md_citations(), baseline)
    assert not new, (
        "Prose cites markdown document(s) that do not exist in this repo. A "
        "citation a reader cannot follow is an unfalsifiable claim -- write "
        "the document, cite one that exists, or make the point without a "
        "citation.\n\n" + _report(new)
    )


def test_no_new_unsupported_ticket_quotes(baseline: Baseline) -> None:
    """Text quoted and attributed to a ticket occurs in that ticket."""
    new = _unregistered(C.find_unsupported_ticket_quotes(), baseline)
    assert not new, (
        "Text is quoted as a ticket's own words but does not appear in that "
        "ticket's entry in docs/tickets.json. A ticket's contract is what "
        "docs/tickets.json says; it cannot be widened, narrowed or granted "
        "an exemption by a docstring that quotes language the ticket does "
        "not contain.\n\n" + _report(new)
    )


def test_no_new_undisclosed_protected_changes(baseline: Baseline) -> None:
    """A commit claiming only mechanical work does not rewrite the plan or
    the harness without saying so."""
    new = _unregistered(C.find_undisclosed_protected_changes(), baseline)
    assert not new, (
        "Commit(s) whose message describes only a mechanical change also "
        "rewrote a PROTECTED path without naming it. Protected paths are "
        "the plan and the harness that enforces it: what they contain is "
        "only reviewable if the commit message says they changed.\n\n" + _report(new)
    )


def test_baseline_has_no_stale_entries(baseline: Baseline) -> None:
    """A registered defect that no longer reproduces must be deleted from
    the register.

    This is what stops known_citation_defects.json from becoming the thing
    it exists to prevent: a list that quietly grants permanent permission.
    A repaired defect's entry has to go, which means someone has to look."""
    live = {f.key() for f in C.all_findings()}
    stale = []
    for key, entry in baseline.items():
        check, where, _ = key
        if check == C.CHECK_COMMIT and not C.commit_is_reachable(where):
            # Not applicable on a branch that does not contain the commit.
            continue
        if key not in live:
            stale.append((key, entry.get("remediation", "")))
    assert not stale, (
        "known_citation_defects.json registers defect(s) that no longer "
        "reproduce. Delete each entry below -- the register must only ever "
        "list live, unrepaired defects.\n\n"
        + "\n".join(f"  {k}\n    was awaiting: {r}" for k, r in stale)
    )


def test_the_real_scan_is_not_vacuous() -> None:
    """The detectors actually read this repo.

    Every check above passes trivially if the corpus is empty, and all
    three did exactly that during development. These floors sit well below
    the measured numbers at the time of writing -- 156 prose files, 40
    markdown documents, 32 tickets, 145 commits, 38 ticket-attributed
    quotations and 99 markdown citations -- and exist only to catch a scan
    that has stopped seeing anything at all."""
    prose_files = C.iter_prose_files()
    assert len(prose_files) > 100, (
        f"prose scan found only {len(prose_files)} source files under "
        f"{C.PROSE_ROOTS} -- the citation checks cannot have examined this "
        f"repo. Check C.REPO_ROOT ({C.REPO_ROOT}) and _SKIP_DIRS."
    )
    assert C.REPO_ROOT.joinpath("docs", "tickets.json").exists()
    assert len(C.existing_md_documents()) > 20
    assert len(C.load_tickets()) > 20
    assert len(C.iter_commits()) > 50

    # And the corpus really does contain the constructs being checked --
    # otherwise a detector could be broken without any test noticing.
    quoted = sum(1 for p in prose_files for _ in C.iter_attributions(C.extract_prose(p).text))
    assert quoted > 5, (
        f"only {quoted} ticket-attributed quotations found across the whole "
        f"repo; the attribution matcher is probably broken."
    )
    assert C.substantive_protected_patterns(), (
        "no protected path patterns parsed out of harness_lib.py"
    )


def test_registered_defects_all_still_reproduce_exactly_once(baseline: Baseline) -> None:
    """Each register entry corresponds to a live finding (the mirror of
    test_baseline_has_no_stale_entries, asserted per check so a wholesale
    detector failure cannot make both tests pass)."""
    live = {f.key() for f in C.all_findings()}
    for check in C.ALL_CHECKS:
        registered = {k for k in baseline if k[0] == check}
        if not registered:
            continue
        applicable = {
            k for k in registered if check != C.CHECK_COMMIT or C.commit_is_reachable(k[1])
        }
        assert applicable <= live, (
            f"{check}: registered defect(s) not reported by the detector -- "
            f"either they were repaired (delete the register entry) or the "
            f"detector stopped detecting them (fix the detector).\n"
            f"  {sorted(applicable - live)}"
        )


# --------------------------------------------------------------------------
# The detectors detect (synthetic)
# --------------------------------------------------------------------------


def _write(tmp_path: Path, rel: str, text: str) -> Path:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture()
def fake_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(C, "REPO_ROOT", tmp_path)
    return tmp_path


def test_detects_a_citation_to_a_document_that_does_not_exist(fake_root: Path) -> None:
    src = _write(fake_root, "pkg/mod.py", '"""See NOTHING-HERE.md for why."""\n')
    findings = C.find_dangling_md_citations([src])
    assert [f.subject for f in findings] == ["NOTHING-HERE.md"]


def test_accepts_a_citation_to_a_document_that_exists(fake_root: Path) -> None:
    _write(fake_root, "docs/REAL.md", "# real\n")
    src = _write(fake_root, "pkg/mod.py", '"""See docs/REAL.md and REAL.md."""\n')
    assert C.find_dangling_md_citations([src]) == []


def test_accepts_a_citation_broken_across_a_hyphenated_line_wrap(fake_root: Path) -> None:
    """The pattern in backend/src/ingress/models.py: 'docs/zendesk-' at the
    end of one line, 'runbook.md' at the start of the next."""
    _write(fake_root, "docs/zendesk-runbook.md", "# runbook\n")
    src = _write(
        fake_root,
        "pkg/mod.py",
        '"""The trigger body is documented in docs/zendesk-\nrunbook.md today."""\n',
    )
    assert C.find_dangling_md_citations([src]) == []


def test_ignores_a_markdown_path_a_test_creates_at_runtime(fake_root: Path) -> None:
    """Only prose is scanned. ``tmp_path / "scratch.md"`` in code is a file
    the test authors, not a claim that a document exists."""
    src = _write(
        fake_root,
        "pkg/mod.py",
        'def t(tmp_path):\n    (tmp_path / "scratch.md").write_text("x")\n',
    )
    assert C.find_dangling_md_citations([src]) == []


_TICKETS = [
    {
        "id": "T-99",
        "objective": "Make the widget resilient.",
        "acceptance": ["the widget retries twice before giving up"],
        "non_goals": ["No change to the widget's public interface"],
    }
]


def test_detects_a_quote_attributed_to_a_ticket_that_does_not_say_it(fake_root: Path) -> None:
    """The T-28 shape: an exemption invented, quoted, and attributed."""
    src = _write(
        fake_root,
        "pkg/test_thing.py",
        '"""Per T-99\'s own escape valve ("if a case is unreachable, say so '
        'rather than inventing one"), this file ships red."""\n',
    )
    findings = C.find_unsupported_ticket_quotes([src], _TICKETS)
    assert len(findings) == 1
    assert findings[0].subject.startswith("T-99: if a case is unreachable")


def test_accepts_a_quote_the_ticket_actually_contains(fake_root: Path) -> None:
    src = _write(
        fake_root,
        "pkg/test_thing.py",
        '"""T-99\'s acceptance says "the widget retries twice before giving '
        'up", so two is the number."""\n',
    )
    assert C.find_unsupported_ticket_quotes([src], _TICKETS) == []


def test_accepts_a_quote_with_a_signposted_bracketed_insertion(fake_root: Path) -> None:
    """ "[...]" is the quoting author's own clarification, not the source's
    words -- the form used in test_status_field.py for T-22's objective."""
    src = _write(
        fake_root,
        "pkg/test_thing.py",
        '"""T-99\'s acceptance: "the widget retries twice [i.e. three '
        'attempts] before giving up"."""\n',
    )
    assert C.find_unsupported_ticket_quotes([src], _TICKETS) == []


def test_a_quote_binds_to_the_nearest_ticket_id_not_the_first(fake_root: Path) -> None:
    """backend/src/agent/escalation_seam.py opens "The T-6 ... seam." and
    then quotes T-5's non-goal; the quote belongs to T-5."""
    src = _write(
        fake_root,
        "pkg/mod.py",
        '"""The T-1 subsystem seam. T-99\'s non-goal is explicit: "No change '
        'to the widget\'s public interface"."""\n',
    )
    assert C.find_unsupported_ticket_quotes([src], _TICKETS) == []


def test_a_quote_attributed_to_a_document_is_not_charged_to_a_ticket(fake_root: Path) -> None:
    """backend/tests/plan/test_ingest_doc.py quotes an OLD version of
    docs/INGEST.md in a paragraph that also names T-26."""
    src = _write(
        fake_root,
        "pkg/mod.py",
        '"""The pre-T-99 version of docs/GUIDE.md\'s step 4 read "confirm '
        'that every widget is listed and unblocked"."""\n',
    )
    assert C.find_unsupported_ticket_quotes([src], _TICKETS) == []


def test_an_unattributed_quotation_is_not_checked(fake_root: Path) -> None:
    """A quotation nobody claims is the ticket's own words is none of this
    check's business -- e.g. quoting a SPEC requirement id."""
    src = _write(
        fake_root,
        "pkg/mod.py",
        '"""T-99 is about resilience. R11: "OFF (default): autonomous send." '
        'No settings row must behave like OFF."""\n',
    )
    assert C.find_unsupported_ticket_quotes([src], _TICKETS) == []


def test_detects_a_quote_attributed_to_a_ticket_that_does_not_exist(fake_root: Path) -> None:
    src = _write(
        fake_root,
        "pkg/mod.py",
        '"""T-404\'s acceptance says "the widget retries twice before giving '
        'up" so we do that."""\n',
    )
    findings = C.find_unsupported_ticket_quotes([src], _TICKETS)
    assert len(findings) == 1
    assert "does not exist in docs/tickets.json" in findings[0].detail


_HARNESS = ".claude/scripts/harness_lib.py"


def test_detects_a_chore_commit_that_rewrites_the_harness() -> None:
    """The 61d26de shape."""
    c = {
        "sha": "a" * 40,
        "subject": "chore: regenerate docs/TASKS.md",
        "body": "",
        "files": [("docs/TASKS.md", 2), (_HARNESS, 100)],
    }
    findings = C.find_undisclosed_protected_changes([c])
    assert [f.subject for f in findings] == [_HARNESS]


def test_accepts_a_chore_commit_that_names_the_file_it_rewrites() -> None:
    c = {
        "sha": "b" * 40,
        "subject": "chore: reformat harness_lib.py",
        "body": "",
        "files": [(_HARNESS, 100)],
    }
    assert C.find_undisclosed_protected_changes([c]) == []


def test_accepts_a_chore_commit_that_names_the_file_only_in_its_body() -> None:
    c = {
        "sha": "c" * 40,
        "subject": "chore: tidy up",
        "body": "Reindents .claude/scripts/harness_lib.py; no behaviour change.",
        "files": [(_HARNESS, 100)],
    }
    assert C.find_undisclosed_protected_changes([c]) == []


def test_accepts_a_commit_whose_subject_claims_real_work() -> None:
    """'harness: close the rename and claim-attribution laundering routes'
    (3faa744) changes 245 lines of harness_lib.py and says so."""
    c = {
        "sha": "d" * 40,
        "subject": "harness: close the rename laundering route",
        "body": "",
        "files": [(_HARNESS, 245)],
    }
    assert C.find_undisclosed_protected_changes([c]) == []


def test_accepts_a_state_sync_commit_that_only_touches_harness_state() -> None:
    """Claims and receipts are PROTECTED but are written by the harness at
    every ticket boundary; 'chore: sync harness state' describes that
    accurately."""
    c = {
        "sha": "e" * 40,
        "subject": "chore: sync harness state after T-29",
        "body": "",
        "files": [
            (".claude/claims/abc.json", 8),
            (".claude/evidence/T-29.json", 20),
            (".claude/monitor/heartbeat.jsonl", 1067),
        ],
    }
    assert C.find_undisclosed_protected_changes([c]) == []


def test_a_one_line_undisclosed_edit_is_below_the_substantive_floor() -> None:
    c = {
        "sha": "f" * 40,
        "subject": "chore: regenerate docs/TASKS.md",
        "body": "",
        "files": [(_HARNESS, 1)],
    }
    assert C.find_undisclosed_protected_changes([c]) == []
    assert C.find_undisclosed_protected_changes([c], min_lines=1)


def test_protected_paths_come_from_the_live_harness_not_a_copy() -> None:
    """If harness_lib.PROTECTED gains a path, this check covers it without
    anyone editing this file."""
    prefixes = {C._regex_literal_prefix(r) for r in C._harness_constant("PROTECTED")}
    assert "docs/tickets.json" in prefixes
    assert ".claude/scripts/" in prefixes
    covered = {p.pattern for p in C.substantive_protected_patterns()}
    assert any("scripts" in p for p in covered)
    assert not any("claims" in p for p in covered)
    assert not any("evidence" in p for p in covered)
