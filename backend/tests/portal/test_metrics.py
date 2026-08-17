"""GET /api/metrics — DESIGN §Metric definitions, SPEC R13, pinned
verbatim:

    human_avoidance_rate = tickets solved via auto-sent replies only /
    all tickets reaching a terminal handling (solved or escalated). Gated
    approved sends and escalations count against it.
    latency = webhook receipt -> public reply posted, autonomous mode only.

The fixture below is hand-built specifically so the pinned formula and the
"wrong" one (folding gated_sent into the numerator too, as if an
approved-through-the-gate send were as autonomous as a real auto-send)
produce DIFFERENT numbers on the SAME data — a test that would pass either
way proves nothing about which interpretation the endpoint implements.
Rows are built directly through ``agent.store.record_run``/``record_draft``
(the exact functions ``agent.nodes.act`` calls), not through the portal's
own approve/reject endpoints, so every outcome/timestamp combination in
the fixture is pinned exactly rather than derived from a flow this file
would then be testing circularly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from agent.store import record_draft, record_run
from escalation.schemas import Reason

from .conftest import AUTH_HEADERS

_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)

# outcome -> the draft.status a real run producing that outcome would have
# left behind (agent.store's own docstring: "auto_sent" for every graph-run
# send, portal-driven "approved"/"rejected" otherwise). Not read by the
# metrics endpoint itself (it only reads runs.outcome) — kept accurate here
# purely so the fixture rows are internally consistent, in case a future
# test reads drafts.status too.
_DRAFT_STATUS_FOR_OUTCOME = {
    "auto_sent": "auto_sent",
    "escalated": "auto_sent",
    "off_topic": "auto_sent",
    "gated_sent": "approved",
    "rejected": "rejected",
    None: "pending",
}


def _seed(
    *,
    ticket_id: str,
    outcome: str | None,
    latency_s: float | None,
    route: str,
    reasons: list[Reason] | None = None,
) -> int:
    received_at = _EPOCH
    replied_at = _EPOCH + timedelta(seconds=latency_s) if latency_s is not None else None
    run_id = record_run(
        ticket_id=ticket_id,
        route=route,
        confidence=0.9,
        outcome=outcome,
        verifier_score=0.9,
        trace_id=f"trace-{ticket_id}",
        received_at=received_at,
        replied_at=replied_at,
        reasons=reasons,
    )
    record_draft(run_id=run_id, body="draft body", status=_DRAFT_STATUS_FOR_OUTCOME[outcome])
    return run_id


def _build_hand_computed_fixture() -> None:
    """5 ``auto_sent`` (latencies 10/20/30/40/50s), 1 ``gated_sent``, 2
    ``escalated``, 1 ``off_topic``, 1 ``rejected``, 1 still-pending
    (``outcome IS NULL``).

    Hand computation (worked by hand, not by the code under test):

        numerator   (auto_sent only)                    = 5
        denominator (auto_sent + gated_sent + escalated) = 5 + 1 + 2 = 8
        human_avoidance_rate                             = 5 / 8 = 0.625

    WRONG interpretation (gated_sent folded into the numerator, i.e.
    treating an approved-through-the-gate send as if it were autonomous):

        (5 + 1) / 8 = 0.75  -- a DIFFERENT number from 0.625.

    Only 0.625 is correct per R12 / DESIGN's "Gated ... sends SHALL be ...
    excluded from the human-avoidance numerator" — the gated_sent run
    counts in the denominator (it did reach a terminal, solved handling)
    but never the numerator.

    ``off_topic``, ``rejected``, and the still-pending run are in NEITHER
    the numerator nor the denominator: none of the three is a "terminal
    handling (solved or escalated)" — off_topic tickets are left ``open``
    (never marked solved), a rejected draft sent nothing, and a pending
    run hasn't been decided yet.

    Latency (hand-computed via linear-interpolation percentile, the same
    method ``numpy.percentile``'s "linear" default uses, over the 5
    ``auto_sent`` latencies ``[10, 20, 30, 40, 50]``):

        rank(p50) = (5-1) * 0.50 = 2.0  -> values[2]                = 30.0
        rank(p95) = (5-1) * 0.95 = 3.8  -> values[3] + (values[4]-values[3])*0.8
                                         = 40 + (50-40)*0.8          = 48.0

    The ``gated_sent`` (5s) and ``off_topic`` (1s) latencies are seeded
    deliberately low and are NOT ``auto_sent`` — if the endpoint wrongly
    folded them into the latency sample, p50/p95 would come out lower than
    30.0/48.0, so this fixture would also catch that bug.

    The 2 ``escalated`` runs carry DIFFERENT reasons — ``metrics-escalated-1``
    just ``"billing"``, ``metrics-escalated-2`` BOTH ``"frustration"`` and
    ``"complexity"`` (a genuinely multi-reason run, per DESIGN's
    ``EscalationCall.reasons: list[Reason]``) — specifically so
    ``test_escalations_by_reason_counts`` below can assert the exact
    per-reason breakdown, not just a total. See that test's own docstring
    for why this also proves the breakdown is real rather than the single
    placeholder bucket ``portal.service`` used to report here.
    """
    for i, latency in enumerate([10.0, 20.0, 30.0, 40.0, 50.0], start=1):
        _seed(ticket_id=f"metrics-auto-{i}", outcome="auto_sent", latency_s=latency, route="kb")
    _seed(ticket_id="metrics-gated-1", outcome="gated_sent", latency_s=5.0, route="kb")
    _seed(
        ticket_id="metrics-escalated-1",
        outcome="escalated",
        latency_s=None,
        route="kb",
        reasons=["billing"],
    )
    _seed(
        ticket_id="metrics-escalated-2",
        outcome="escalated",
        latency_s=None,
        route="case_status",
        reasons=["frustration", "complexity"],
    )
    _seed(ticket_id="metrics-offtopic-1", outcome="off_topic", latency_s=1.0, route="off_topic")
    _seed(ticket_id="metrics-rejected-1", outcome="rejected", latency_s=None, route="kb")
    _seed(ticket_id="metrics-pending-1", outcome=None, latency_s=None, route="kb")


def test_human_avoidance_rate_excludes_gated_sends_from_numerator_but_counts_them_in_denominator(
    client: TestClient,
) -> None:
    _build_hand_computed_fixture()

    response = client.get("/api/metrics", headers=AUTH_HEADERS)
    assert response.status_code == 200
    payload = response.json()

    assert payload["human_avoidance_rate"] == 0.625

    # The two interpretations genuinely differ on this fixture -- proof
    # this isn't a test that would pass either way.
    wrong_rate_if_gated_counted_as_human_avoided = 6 / 8
    assert payload["human_avoidance_rate"] != wrong_rate_if_gated_counted_as_human_avoided


def test_latency_percentiles_computed_only_over_autonomous_sends(client: TestClient) -> None:
    _build_hand_computed_fixture()

    response = client.get("/api/metrics", headers=AUTH_HEADERS)
    assert response.status_code == 200
    payload = response.json()

    assert payload["latency_p50_s"] == 30.0
    assert payload["latency_p95_s"] == 48.0


def test_escalations_by_reason_counts(client: TestClient) -> None:
    """SPEC R13 / DESIGN's ``escalations_by_reason`` is a real per-reason
    breakdown (``runs.reasons``, threaded end to end from the escalation
    decision — see ``portal.service.compute_metrics``'s own docstring), not
    a single bucket. ``_build_hand_computed_fixture`` seeds exactly 2
    escalated runs: ``metrics-escalated-1`` with reason ``billing`` alone,
    and ``metrics-escalated-2`` with BOTH ``frustration`` and
    ``complexity`` — a genuinely multi-reason run.

    This test asserts the EXACT dict, hand-computed:

        {"billing": 1, "frustration": 1, "complexity": 1}

    A prior version of this endpoint bucketed every escalated run under a
    single ``"unspecified"`` placeholder key (the reason was never
    persisted at all) — on this exact fixture that placeholder behavior
    would have produced ``{"unspecified": 2}``, a dict with none of the
    keys asserted below and a different sum (2, not 3). This test fails
    outright under that old behavior, proving the per-reason wiring is
    real, not just present."""
    _build_hand_computed_fixture()

    response = client.get("/api/metrics", headers=AUTH_HEADERS)
    assert response.status_code == 200
    payload = response.json()

    assert payload["escalations_by_reason"] == {
        "billing": 1,
        "frustration": 1,
        "complexity": 1,
    }

    # The sum (3) exceeds the number of escalated runs (2) precisely
    # because metrics-escalated-2 carries TWO reasons and is counted under
    # both — correct per DESIGN (see compute_metrics's docstring), and a
    # number the single-bucket placeholder could never have produced.
    assert sum(payload["escalations_by_reason"].values()) == 3


def test_metrics_with_no_runs_is_well_defined(client: TestClient) -> None:
    """The endpoint is defined on an empty database — 200, exact body, no
    500 and no NaN.

    The two latency values in this body changed under W2-C3. They were
    ``0.0``, asserted here since T-8 (commit 67776ba). ``0.0`` is not a
    weaker version of the right answer, it is a different and false one: a
    percentile over an empty sample does not exist, and reporting it as zero
    seconds makes SPEC success criterion 6 ("p95 < 5 min") *vacuously true*
    read off the panel — `docs/STATE.md §4.1` recorded exactly that, and
    `docs/BUILD-PLAN.md §4 Track C` C3 decided the replacement: "return
    ``null`` rather than ``0.0`` for percentiles over an empty sample", with
    the acceptance "``/api/metrics`` on an empty database does **not**
    report a passing p95". ``sample_count`` is the other half — a p95 is
    only as meaningful as the number of runs behind it, and until now the
    payload gave a reader no way to ask.

    ``human_avoidance_rate`` deliberately stays ``0.0``: it is a ratio over
    a denominator that is genuinely zero, not a percentile over an empty
    sample, and ``sample_count`` reports the size of the *latency* sample.
    """
    response = client.get("/api/metrics", headers=AUTH_HEADERS)
    assert response.status_code == 200
    assert response.json() == {
        "human_avoidance_rate": 0.0,
        "latency_p50_s": None,
        "latency_p95_s": None,
        "sample_count": 0,
        "escalations_by_reason": {},
    }


def test_an_empty_database_cannot_report_a_passing_p95(client: TestClient) -> None:
    """W2-C3's acceptance, stated as the thing it forbids.

    SPEC success criterion 6 is "p95 < 5 min". With no runs at all, the
    endpoint used to report ``latency_p95_s = 0.0`` — which satisfies that
    criterion, and satisfies it *best of all possible values*. A grader
    reading the metrics panel on a freshly deployed stack would have seen
    the strongest possible evidence for a claim that nothing had been
    measured. This test is the assertion that a number nobody measured
    cannot be read as a passing one.
    """
    payload = client.get("/api/metrics", headers=AUTH_HEADERS).json()

    # The forbidden outcome first, stated as a property rather than as a
    # value, so the failure message names the defect instead of a missing
    # key: whatever the endpoint returns, it must not be a number that
    # passes R8's threshold.
    p95 = payload["latency_p95_s"]
    assert not (isinstance(p95, int | float) and not isinstance(p95, bool) and p95 < 300), (
        f"an empty run history reported latency_p95_s={p95!r}, which reads as "
        f"a PASS against SPEC success criterion 6 (p95 < 5 min) while nothing "
        f"has ever been measured"
    )
    assert p95 is None
    assert payload["sample_count"] == 0


def test_sample_count_is_the_number_of_runs_the_percentiles_rest_on(
    client: TestClient,
) -> None:
    """A p95 over 5 points and a p95 over 500 are different claims, and the
    payload said nothing that let a reader tell them apart.

    The fixture has 5 ``auto_sent`` runs and 4 more runs with other outcomes
    (one of which, ``gated_sent``, even has a latency). ``sample_count`` must
    be 5 — the size of the sample the percentiles were actually computed
    over — not 9 or 10. Asserting 5 against that fixture is what separates
    "the latency sample" from "the runs table".
    """
    _build_hand_computed_fixture()

    payload = client.get("/api/metrics", headers=AUTH_HEADERS).json()

    assert payload["sample_count"] == 5
    assert payload["latency_p50_s"] == 30.0
    assert payload["latency_p95_s"] == 48.0


def test_one_run_is_reported_as_one_run_and_not_as_a_percentile_estimate(
    client: TestClient,
) -> None:
    """The interesting boundary is 1, not 0: a single run produces a real,
    non-null p95 that happens to equal that one measurement. That is correct
    and it is also the number most likely to be over-read, so the count has
    to travel with it."""
    _seed(ticket_id="metrics-single-1", outcome="auto_sent", latency_s=12.0, route="kb")

    payload = client.get("/api/metrics", headers=AUTH_HEADERS).json()

    assert payload["sample_count"] == 1
    assert payload["latency_p50_s"] == 12.0
    assert payload["latency_p95_s"] == 12.0
