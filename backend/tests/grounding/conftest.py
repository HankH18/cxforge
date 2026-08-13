"""Shared fixtures and structural assertion helpers for the grounding suite
(R9 — DESIGN's "Decisions & rationale": "Templates for case facts, not free
generation — the only way 'zero hallucinated case facts' becomes testable").

Real Postgres (same rationale as ``backend/tests/graph/conftest.py``), a
fake ``LLMClient``, and ``EmailAdapter`` reused as the ``HelpdeskPort``
fake. Skips itself when ``SKIP_DB_TESTS=1``, mirroring every other
DB-backed suite in this repo.

The structural extractors below (``extract_case_ids``/``extract_stage_
mentions``/``assert_case_facts_trace_to``/``assert_no_case_facts_present``)
delegate to ``agent.grounding_guard`` — the SAME shape detectors T-5's
deterministic R9 guard uses to force escalation on a fabricated free-
generated draft (see that module's docstring for the full rationale and its
honestly-stated residual risk). Sharing the detectors rather than
maintaining a second, narrower regex set here means this test suite's own
"did a fabrication leak" assertions can never silently recognize less than
the guard itself does — a fabrication shaped in a form the guard added
support for is automatically also a form these assertions catch.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
import yaml

from agent.grounding_guard import (
    extract_case_ids_loose,
    extract_dna_availability_claim,
    extract_eta_weeks_claims,
    extract_photos_availability_claim,
    extract_stage_claims,
)
from data import Case, get_connection
from data.seed import DEFAULT_CASES_PATH, SeedResult, seed_all
from helpdesk.email_adapter import EmailAdapter

pytestmark = pytest.mark.skipif(
    os.getenv("SKIP_DB_TESTS") == "1",
    reason="requires the docker-compose db service (CI sets SKIP_DB_TESTS=1)",
)


@pytest.fixture(scope="session")
def seeded() -> SeedResult:
    return seed_all()


@pytest.fixture(scope="session")
def fixture_cases() -> list[dict[str, Any]]:
    """Raw case rows straight from ``fixtures/cases.yaml`` — the adversarial
    tests need the full real-case-id universe to prove none of it leaked,
    not just the one case a given test is about."""
    payload = yaml.safe_load(DEFAULT_CASES_PATH.read_text(encoding="utf-8"))
    return list(payload["cases"])


@pytest.fixture(autouse=True)
def _clean_run_tables(seeded: SeedResult) -> None:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE drafts, runs, settings RESTART IDENTITY")


@pytest.fixture
def port() -> EmailAdapter:
    return EmailAdapter()


def seed_conversation(port: EmailAdapter, *, requester_email: str, message: str) -> str:
    ticket_id = port.seed_ticket(requester_email=requester_email)
    port.seed_comment(ticket_id, author="customer", text=message)
    return ticket_id


# -- structural grounding assertions (not string-matching a blocklist) ------
#
# ``extract_case_ids``/``extract_stage_mentions`` are thin, stably-named
# re-exports of ``agent.grounding_guard``'s loose/paraphrase-tolerant
# detectors (case-insensitive, separator/grouping-tolerant case ids;
# literal stage words AND their curated indirect phrasings) — kept as
# module-level names here since ``test_adversarial.py`` already imports
# them by these names.

extract_case_ids = extract_case_ids_loose
extract_stage_mentions = extract_stage_claims


def assert_no_case_facts_present(body: str, fixture_cases: list[dict[str, Any]]) -> None:
    """For a run that never resolved ANY case (escalation before a tool
    result existed): assert `body` contains no real case id, no
    pipeline-stage claim (literal word or curated paraphrase), no
    personalized ETA/turnaround claim, and no DNA/photo-availability claim
    — not merely that some hardcoded "bad" string is absent, but that
    nothing shaped like a case fact appears, in any of the forms
    ``agent.grounding_guard`` recognizes."""
    real_case_ids = {row["case_id"] for row in fixture_cases}
    mentioned_ids = extract_case_ids(body)
    leaked_ids = mentioned_ids & real_case_ids
    assert not leaked_ids, f"body names real case id(s) {leaked_ids} with no tool result backing"

    mentioned_stages = extract_stage_mentions(body)
    assert not mentioned_stages, (
        f"body names stage claim(s) {mentioned_stages} with no tool result backing them"
    )

    mentioned_etas = extract_eta_weeks_claims(body)
    assert not mentioned_etas, (
        f"body states turnaround/ETA claim(s) of {mentioned_etas} week(s) with no tool "
        "result backing them"
    )

    dna_claim = extract_dna_availability_claim(body)
    assert dna_claim is None, (
        f"body states a DNA-profile availability claim ({dna_claim}) with no tool "
        "result backing it"
    )

    photos_claim = extract_photos_availability_claim(body)
    assert photos_claim is None, (
        f"body states an accession-photo availability claim ({photos_claim}) with no "
        "tool result backing it"
    )


def assert_case_facts_trace_to(body: str, case: Case) -> None:
    """Positive, structural traceability check (not a blocklist grep):
    parse every case-fact-shaped value actually present in `body` — case
    id (any format ``agent.grounding_guard`` recognizes), stage (literal or
    paraphrased), ETA figure (digits or spelled out), DNA/photo
    availability (the template's fixed sentence OR equivalent free prose)
    — and assert each one equals the corresponding field on `case`: the
    real ``data.Case`` tool result this run resolved. A body naming a
    different real case's id, a different stage (by word or by
    paraphrase), a mismatched ETA figure in any wording, or a mismatched
    availability claim in prose fails here, even though no individual
    "bad" substring was hardcoded to grep for."""
    mentioned_ids = extract_case_ids(body)
    assert mentioned_ids <= {case.case_id}, (
        f"body mentions case id(s) {mentioned_ids - {case.case_id}} that are not "
        f"case.case_id ({case.case_id!r}) from this run's tool result"
    )

    mentioned_stages = extract_stage_mentions(body)
    assert mentioned_stages <= {case.stage}, (
        f"body mentions stage(s) {mentioned_stages - {case.stage}} that don't match "
        f"case.stage ({case.stage!r}) from this run's tool result"
    )

    mentioned_etas = extract_eta_weeks_claims(body)
    if mentioned_etas:
        assert mentioned_etas <= {case.eta_weeks}, (
            f"body states {sorted(mentioned_etas)} week(s) remaining but case.eta_weeks "
            f"is {case.eta_weeks!r}"
        )

    dna_claim = extract_dna_availability_claim(body)
    if dna_claim is not None:
        assert dna_claim == bool(case.dna_profile_available), (
            f"body states DNA profile availability={dna_claim} but "
            f"case.dna_profile_available is {case.dna_profile_available!r}"
        )

    photos_claim = extract_photos_availability_claim(body)
    if photos_claim is not None:
        assert photos_claim == bool(case.photos_available), (
            f"body states accession-photo availability={photos_claim} but "
            f"case.photos_available is {case.photos_available!r}"
        )
