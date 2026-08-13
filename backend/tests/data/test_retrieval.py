"""T-1 acceptance 4: retrieval smoke test — a held-out set of 12 naturally
phrased customer queries, none of which echo their expected doc's title.

The original version of this test used 5 queries built by reading each doc
and restating its title vocabulary ("What is the published expected
turnaround window..." for a doc titled "Expected Turnaround Times..."). That
passed trivially against a purely lexical embedder and gave no signal about
real customer phrasing ("how long until I hear back about my sample"), which
routinely shares almost no vocabulary with the doc that answers it. An
independent audit measured the old fixture content at 0/10 correct at rank 1
and 1/10 in the top 3 against natural phrasings.

The fix (see ``data.chunking.KBDoc.keywords`` and ``data.seed._seed_kb``) is
a curated ``keywords:`` front-matter field per doc — the natural, sometimes
colloquial, phrasings a real customer would type for that doc's topic —
folded into the text handed to the embedder at index time (never into the
stored ``kb_chunks.text``, which T-5's templates and groundedness verifier
read verbatim). The 12 queries below are a held-out set chosen independently
of that keyword authoring; they must not be edited, reworded, or dropped.

Required bar: at least 10 of 12 correct at rank 1, and all 12 in the top 3.
"""

from __future__ import annotations

import os

import pytest

from data.retrieval import search_kb
from data.seed import SeedResult

pytestmark = pytest.mark.skipif(
    os.getenv("SKIP_DB_TESTS") == "1",
    reason="requires the docker-compose db service (CI sets SKIP_DB_TESTS=1)",
)

TOP_K = 3
MIN_RANK_1 = 10

# (query, expected doc_slug) — a held-out natural-phrasing query set, fixed
# verbatim. Do not edit, reword, or drop any entry to make a failure disappear.
QUERY_CASES: list[tuple[str, str]] = [
    ("how long until I hear back about my sample", "turnaround-times"),
    ("can I get my money back", "refund-policy"),
    (
        "the lab said my swab didn't work, what happens now",
        "sample-failure-and-recourse",
    ),
    (
        "someone else needs to be able to call in about this case",
        "case-information-authorization",
    ),
    (
        "is there a person I can talk to about this, not a bot",
        "escalation-and-specialist-requests",
    ),
    ("I moved, where do I send my new address", "updating-contact-details"),
    ("can you make this go faster, I'll pay extra", "requesting-a-rush"),
    (
        "how long do you keep my DNA after you're done",
        "privacy-and-data-retention",
    ),
    ("what do I actually get at the end", "results-delivery-and-formats"),
    (
        "why hasn't anyone found a match yet",
        "genealogy-limitations-and-expectations",
    ),
    ("I got charged twice", "billing-and-payment-terms"),
    ("how do I ship the evidence to you", "sample-submission-chain-of-custody"),
]


def _top_slugs(query: str, k: int) -> list[str]:
    """Distinct doc_slugs among the top-``k`` chunks for ``query``, in rank order."""
    slugs: list[str] = []
    for result in search_kb(query, k=k):
        if result.chunk.doc_slug not in slugs:
            slugs.append(result.chunk.doc_slug)
    return slugs


@pytest.mark.parametrize("query,expected_slug", QUERY_CASES)
def test_retrieval_surfaces_expected_doc_in_top_3(
    seeded: SeedResult, query: str, expected_slug: str
) -> None:
    """All 12 held-out queries must surface their expected doc in the top 3."""
    slugs = _top_slugs(query, k=10)[:TOP_K]
    assert expected_slug in slugs, (
        f"expected {expected_slug!r} in top-{TOP_K} for {query!r}, got {slugs}"
    )


def test_retrieval_gets_at_least_ten_of_twelve_at_rank_1(seeded: SeedResult) -> None:
    """At least 10 of the 12 held-out queries must rank their expected doc first."""
    misses = []
    hits = 0
    for query, expected_slug in QUERY_CASES:
        top_slug = _top_slugs(query, k=1)[0]
        if top_slug == expected_slug:
            hits += 1
        else:
            misses.append((query, expected_slug, top_slug))

    assert hits >= MIN_RANK_1, (
        f"expected at least {MIN_RANK_1}/12 correct at rank 1, got {hits}/12; "
        f"misses={misses}"
    )


def test_query_cases_cover_twelve_distinct_docs() -> None:
    """Guards against the smoke test collapsing onto fewer than 12 distinct docs."""
    assert len(QUERY_CASES) == 12
    assert len({slug for _, slug in QUERY_CASES}) == 12
