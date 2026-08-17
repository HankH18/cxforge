"""W2-C1 (ADR-006) — the Langfuse trace `act` emits for every run.

`docs/STATE.md §6.3`: Langfuse was a declared dependency with zero
``import langfuse`` repo-wide. `act` minted a ``trace_id``, persisted it on
``runs.trace_id``, and told nobody — so every trace link the portal built
pointed at a trace that did not exist.

Three properties carry this feature, and each is tested on its own here
because each has its own way of silently reverting:

* **The trace is keyed on the id `act` already minted** (BUILD-PLAN §1.6).
  A second, freshly minted id would produce a perfectly healthy-looking
  trace in Langfuse that no row in ``runs`` points at — the feature would
  look done and the feed's links would still be dead. So the assertion is
  not "a trace was emitted" but "the emitted trace's id is the one this run
  wrote to the database", read back out of Postgres.

* **It degrades to a real no-op with the keys absent**, so the offline
  suite stays offline. Not "spans are dropped" — the `langfuse` package is
  never imported and no client object is built. There are two independent
  gates (keys, and the pytest gate) and each is tested separately, because
  either one alone would let the other rot unnoticed: with only the key
  check, ``set -a; source .env; set +a; uv run pytest`` would ship every
  fixture run in this file to the real `cxforge` project.

* **The times it reports are measured, not artefacts of when the span
  objects were built.** The trace is assembled post-hoc in `act`, so this is
  the one property the shape actively works against, and it failed silently
  for exactly that reason: a real 6.5s run published as ``latency 0.002s``
  with all eight observations inside a 2ms window. Structure alone cannot
  catch that — every span was present, named and nested correctly — so the
  assertions below are on *durations*: a deliberately slow node's span has
  to be slow, its siblings have to not be, and the whole-trace span has to
  match the ``replied_at - received_at`` interval in ``runs`` that
  ``/api/metrics`` reports.

The double below records what the SDK would have been asked to do. It is
not a Langfuse stand-in and does not pretend to be one: whether Langfuse
accepts these calls is proven by reading a real trace back from the API
(W2-C1's report), which no test can do offline. What it does prove is the
half a live check cannot — that the ids line up, that a span exists for
every node that ran and none for a node that did not, and that the
`compose` span carries the exact `Case` its reply was rendered from.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel

from agent import llm as llm_module
from agent.graph import build_graph, run_agent
from agent.nodes import AgentDeps
from agent.schemas import Classification, GroundednessJudgment, KBAnswerDraft
from data import Case, get_case, get_connection
from data.seed import SeedResult
from escalation.engine import EscalationEngine
from helpdesk.email_adapter import EmailAdapter

from .conftest import seed_conversation, set_gate
from .fakes import FakeLLMClient

# --------------------------------------------------------------------------
# The recording double
# --------------------------------------------------------------------------


class RecordingOtelSpan:
    """Stands in for the OpenTelemetry span a Langfuse observation wraps,
    with the one property this feature depends on: a settable
    ``_start_time`` in nanoseconds, readable back through a read-only
    ``start_time``. That is verbatim the shape of
    ``opentelemetry.sdk.trace.Span``, and it is what
    ``agent.llm._apply_real_start_time`` writes through, because Langfuse's
    ``start_observation`` has no ``start_time`` parameter of its own.

    Mirroring an SDK internal in a double is a real cost, and it is paid on
    purpose: without it these tests could only assert that a span *exists*,
    which is exactly the half of this defect that already looked fine. That
    the real SDK still honours it is proven the only way it can be — by
    reading a trace back from the Langfuse API (W2-C1's report)."""

    def __init__(self) -> None:
        self._start_time: int | None = None

    @property
    def start_time(self) -> int | None:
        return self._start_time


class RecordingSpan:
    """Stands in for ``langfuse.LangfuseSpan`` — records, nests, ends."""

    def __init__(self, name: str, kind: str, payload: dict[str, Any]) -> None:
        self.name = name
        self.kind = kind
        self.input: Any = payload.get("input")
        self.output: Any = payload.get("output")
        self.metadata: Any = payload.get("metadata")
        self.children: list[RecordingSpan] = []
        self.ended = False
        self.end_time_ns: int | None = None
        self._otel_span = RecordingOtelSpan()

    def start_observation(
        self,
        *,
        name: str,
        as_type: str = "span",
        input: Any = None,
        output: Any = None,
        metadata: Any = None,
    ) -> RecordingSpan:
        child = RecordingSpan(
            name, as_type, {"input": input, "output": output, "metadata": metadata}
        )
        self.children.append(child)
        return child

    def end(self, *, end_time: int | None = None) -> None:
        self.ended = True
        self.end_time_ns = end_time

    # -- what the trace will actually show ---------------------------------

    @property
    def start_time_ns(self) -> int | None:
        return self._otel_span.start_time

    @property
    def duration_s(self) -> float | None:
        """The duration Langfuse will report, or ``None`` if this span was
        given no real bounds at all. Deliberately computed from the two
        values handed to the SDK rather than from anything the production
        code calculated — a duration nobody sends cannot be asserted into
        existence."""
        if self.start_time_ns is None or self.end_time_ns is None:
            return None
        return (self.end_time_ns - self.start_time_ns) / 1_000_000_000


class RecordingLangfuse:
    """Stands in for ``langfuse.Langfuse``."""

    def __init__(self, *, fail: bool = False) -> None:
        self.traces: list[tuple[str, RecordingSpan]] = []
        self.flushes = 0
        self._fail = fail

    def start_observation(
        self,
        *,
        trace_context: dict[str, str],
        name: str,
        as_type: str = "span",
        input: Any = None,
        output: Any = None,
        metadata: Any = None,
    ) -> RecordingSpan:
        if self._fail:
            raise RuntimeError("langfuse is having a bad day")
        root = RecordingSpan(
            name, as_type, {"input": input, "output": output, "metadata": metadata}
        )
        self.traces.append((trace_context["trace_id"], root))
        return root

    def flush(self) -> None:
        self.flushes += 1


class ExplodingClientFactory:
    """Fails the test if a client is ever built. This is what "no-op" has to
    mean: not a client that drops spans, but no client at all."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        raise AssertionError(
            "a Langfuse client was constructed even though tracing should be off"
        )


@pytest.fixture(autouse=True)
def _no_cached_client() -> Any:
    """The module caches its client; tests flip the environment under it."""
    llm_module.reset_tracing()
    yield
    llm_module.reset_tracing()


@pytest.fixture
def tracer(monkeypatch: pytest.MonkeyPatch) -> RecordingLangfuse:
    """Both gates open, with the recording double behind them."""
    recorder = RecordingLangfuse()
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-fake-for-tests")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-fake-for-tests")
    monkeypatch.setattr(llm_module, "_running_under_pytest", lambda: False)
    monkeypatch.setattr(llm_module, "_new_client", lambda: recorder)
    llm_module.reset_tracing()
    return recorder


def _persisted_trace_id(ticket_id: str) -> str:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT trace_id FROM runs WHERE ticket_id = %s", (ticket_id,))
        rows = cur.fetchall()
    assert len(rows) == 1, f"expected exactly one run for {ticket_id}, got {rows}"
    return str(rows[0][0])


def _persisted_interval(ticket_id: str) -> tuple[datetime, datetime]:
    """The run's own ``received_at``/``replied_at``, straight out of the row
    `act` wrote — the same two columns ``/api/metrics`` computes its latency
    and ``p95`` from."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT received_at, replied_at FROM runs WHERE ticket_id = %s", (ticket_id,)
        )
        rows = cur.fetchall()
    assert len(rows) == 1, f"expected exactly one run for {ticket_id}, got {rows}"
    received_at, replied_at = rows[0]
    assert replied_at is not None
    return received_at, replied_at


def _epoch_ns(when: datetime) -> int:
    """Nanoseconds since the epoch, computed here independently of
    ``agent.llm``'s own conversion — a test that imported the production
    helper would agree with it about a wrong unit."""
    return int(when.timestamp() * 1_000_000_000)


def _span_named(root: RecordingSpan, name: str) -> RecordingSpan:
    matches = [c for c in root.children if c.name == name]
    assert len(matches) == 1, f"expected one {name!r} span, got {[c.name for c in root.children]}"
    return matches[0]


# --------------------------------------------------------------------------
# Gate 1: the key pair
# --------------------------------------------------------------------------


def test_absent_keys_build_no_client_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = ExplodingClientFactory()
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    # The pytest gate is opened deliberately, so this test is about the keys
    # alone and cannot pass on the strength of the other gate.
    monkeypatch.setattr(llm_module, "_running_under_pytest", lambda: False)
    monkeypatch.setattr(llm_module, "_new_client", factory)

    assert llm_module.langfuse_configured() is False
    assert llm_module.emit_trace(trace_id="deadbeef" * 4, name="agent_run") is False
    assert factory.calls == 0


@pytest.mark.parametrize(
    ("public", "secret"),
    [("pk-lf-only", None), (None, "sk-lf-only"), ("", "sk-lf-real"), ("pk-lf-real", "")],
)
def test_half_a_key_pair_is_not_configuration(
    monkeypatch: pytest.MonkeyPatch, public: str | None, secret: str | None
) -> None:
    """In Langfuse the key PAIR is the project pointer (`docs/STATE.md
    §3.1`), and Cloud authenticates on the secret alone — a client built
    with half a pair traces somewhere nobody intended rather than failing.
    Empty strings are covered too: that is what ``${LANGFUSE_PUBLIC_KEY:-}``
    renders to in both compose files when the variable is unset."""
    factory = ExplodingClientFactory()
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    if public is not None:
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", public)
    if secret is not None:
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", secret)
    monkeypatch.setattr(llm_module, "_running_under_pytest", lambda: False)
    monkeypatch.setattr(llm_module, "_new_client", factory)

    assert llm_module.langfuse_configured() is False
    assert llm_module.emit_trace(trace_id="deadbeef" * 4, name="agent_run") is False
    assert factory.calls == 0


# --------------------------------------------------------------------------
# Gate 2: the suite stays offline even when the keys are real
# --------------------------------------------------------------------------


def test_the_suite_never_traces_even_with_a_real_key_pair_exported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`docs/deploy.md` and this repo's own commands tell an operator to run
    ``set -a; source .env; set +a`` first, and `.env` holds a live
    ``pk-lf-``/``sk-lf-`` pair. Without this gate, running the suite that
    way would ship every fixture run in `backend/tests/graph` to the real
    `cxforge` project — the offline suite making vendor network calls."""
    factory = ExplodingClientFactory()
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-looks-completely-real")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-looks-completely-real")
    monkeypatch.setattr(llm_module, "_new_client", factory)

    # Deliberately NOT patching _running_under_pytest: we are under pytest.
    assert llm_module.langfuse_configured() is True
    assert llm_module._running_under_pytest() is True
    assert llm_module.emit_trace(trace_id="deadbeef" * 4, name="agent_run") is False
    assert factory.calls == 0


# --------------------------------------------------------------------------
# The trace a real run produces
# --------------------------------------------------------------------------


def test_the_trace_is_keyed_on_the_id_the_run_persisted(
    seeded: SeedResult, port: EmailAdapter, tracer: RecordingLangfuse
) -> None:
    """BUILD-PLAN §1.6: "spans key on the ``trace_id`` already minted in
    ``act`` — do not mint a second one". A second id traces fine and links
    to nothing, which is indistinguishable from success unless the ids are
    compared against the row `act` wrote."""
    case_id = "MFG-2025-0734"
    case = get_case(case_id)
    assert isinstance(case, Case)
    ticket_id = seed_conversation(
        port,
        requester_email=case.requester_email,
        message=f"What is the status of case {case_id}?",
    )
    llm = FakeLLMClient(
        responses={
            Classification: Classification(
                topic="case status inquiry", route="case_status", case_id=case_id, confidence=0.97
            )
        }
    )

    run_agent(ticket_id, port=port, llm=llm)

    assert len(tracer.traces) == 1, "exactly one trace per run"
    emitted_id, root = tracer.traces[0]
    assert emitted_id == _persisted_trace_id(ticket_id)
    assert root.name == "agent_run"
    assert root.ended is True
    assert tracer.flushes == 1


def test_the_compose_span_shows_the_case_feeding_the_templated_reply(
    seeded: SeedResult, port: EmailAdapter, tracer: RecordingLangfuse
) -> None:
    """ADR-006's acceptance, and the whole reason the trace is worth
    building: "a real run produces a Langfuse trace whose spans show the
    ``Case`` tool result feeding the templated reply".

    So the assertion is a join, not a presence check — the exact case fields
    that appear as the ``case_status`` tool span's OUTPUT must also be the
    ``compose`` span's INPUT, and the string `compose` emitted must quote
    them. A trace where `compose` only showed its output would look fine and
    prove nothing about where the facts came from."""
    case_id = "MFG-2025-0734"
    case = get_case(case_id)
    assert isinstance(case, Case)
    ticket_id = seed_conversation(
        port,
        requester_email=case.requester_email,
        message=f"What is the status of case {case_id}?",
    )
    llm = FakeLLMClient(
        responses={
            Classification: Classification(
                topic="case status inquiry", route="case_status", case_id=case_id, confidence=0.97
            )
        }
    )

    run_agent(ticket_id, port=port, llm=llm)
    _, root = tracer.traces[0]

    tool_case = _span_named(root, "case_status").output["case"]
    assert tool_case["case_id"] == case_id
    assert tool_case["stage"] == case.stage

    compose = _span_named(root, "compose")
    assert compose.input["case"] == tool_case
    assert compose.input["generated_by"] == "template"

    draft = compose.output["draft"]
    assert case_id in draft
    assert case.stage in draft
    assert str(case.eta_weeks) in draft

    # And the reply the customer actually received is that same string.
    assert port.transport.sent[0].html_body.count(case_id) >= 1


def test_spans_cover_every_node_that_ran_and_no_node_that_did_not(
    seeded: SeedResult, port: EmailAdapter, tracer: RecordingLangfuse
) -> None:
    """Spans are driven by the run's own ``actions`` list, so the trace is a
    record of what happened rather than a fixed picture of the pipeline. A
    `case_status` run must not sprout a `kb_answer` span it never executed."""
    case_id = "MFG-2025-0734"
    case = get_case(case_id)
    assert isinstance(case, Case)
    ticket_id = seed_conversation(
        port, requester_email=case.requester_email, message=f"status of {case_id}?"
    )
    llm = FakeLLMClient(
        responses={
            Classification: Classification(
                topic="case status inquiry", route="case_status", case_id=case_id, confidence=0.97
            )
        }
    )

    result = run_agent(ticket_id, port=port, llm=llm)
    _, root = tracer.traces[0]
    span_names = [c.name for c in root.children]

    assert span_names == [a for a in result["actions"] if not a.startswith(("port:", "gate:"))]
    assert span_names == ["ingest", "classify", "case_status", "compose", "verify", "decide", "act"]
    assert "kb_answer" not in span_names
    assert "permission" not in span_names
    assert all(c.ended for c in root.children)


def test_a_kb_run_traces_retrieval_and_the_verifier_score(
    seeded: SeedResult, port: EmailAdapter, tracer: RecordingLangfuse
) -> None:
    """The mirror image: the `kb` route is the only one that free-generates,
    so it is the only one where `verify` actually scores anything. The trace
    has to show that difference rather than a uniform template."""
    ticket_id = seed_conversation(
        port,
        requester_email="curious@example.com",
        message="How does forensic genetic genealogy work?",
    )
    llm = FakeLLMClient(
        responses={
            Classification: Classification(
                topic="how forensic genetic genealogy works",
                route="kb",
                case_id=None,
                confidence=0.88,
            ),
            KBAnswerDraft: KBAnswerDraft(
                answer="Forensic genetic genealogy compares a DNA profile against "
                "public genealogy databases to build a family tree."
            ),
            GroundednessJudgment: GroundednessJudgment(
                score=0.95, rationale="every claim traces to a retrieved chunk"
            ),
        }
    )

    run_agent(ticket_id, port=port, llm=llm)
    _, root = tracer.traces[0]
    span_names = [c.name for c in root.children]

    assert "kb_answer" in span_names
    assert "case_status" not in span_names

    retrieved = _span_named(root, "kb_answer").output["retrieved_chunks"]
    assert retrieved, "a kb run that retrieved nothing escalates; this one did not"
    assert {"doc_slug", "chunk_index", "score"} == set(retrieved[0])

    verify = _span_named(root, "verify")
    assert verify.kind == "evaluator"
    assert verify.output["verifier_score"] == 0.95
    assert verify.output["ran"] is True

    compose = _span_named(root, "compose")
    assert compose.input["generated_by"] == "llm"


def test_a_gated_run_traces_too_and_says_nothing_was_sent(
    seeded: SeedResult, port: EmailAdapter, tracer: RecordingLangfuse
) -> None:
    """R11's gate returns from `act` early, before the port calls. That early
    return is exactly the kind of path an "add tracing at the end" change
    forgets, and a held draft is the run a reviewer most wants to inspect."""
    set_gate(True)
    case_id = "MFG-2025-0734"
    case = get_case(case_id)
    assert isinstance(case, Case)
    ticket_id = seed_conversation(
        port, requester_email=case.requester_email, message=f"status of {case_id}?"
    )
    llm = FakeLLMClient(
        responses={
            Classification: Classification(
                topic="case status inquiry", route="case_status", case_id=case_id, confidence=0.97
            )
        }
    )

    run_agent(ticket_id, port=port, llm=llm)

    assert port.transport.sent == [], "the gate is ON; nothing may be sent"
    assert len(tracer.traces) == 1
    emitted_id, root = tracer.traces[0]
    assert emitted_id == _persisted_trace_id(ticket_id)
    assert root.output["outcome"] is None
    assert root.metadata["gate_enabled"] is True
    # No reply was posted, so there is no receipt->reply interval to report.
    # It is absent rather than 0.0 — the same honesty C3 applies to
    # `/api/metrics`: a latency nobody measured must not read as a fast one.
    assert "replied_at" not in root.metadata
    assert "latency_s" not in root.metadata
    assert _span_named(root, "act").output["actions"][-1] == "gate:held_pending"


def test_the_trace_metadata_carries_the_same_latency_the_run_recorded(
    seeded: SeedResult, port: EmailAdapter, tracer: RecordingLangfuse
) -> None:
    """ADR-004 corrected what ``received_at`` means. The trace and
    ``/api/metrics`` must never be able to disagree about it, so the trace
    reports the two timestamps `act` wrote to ``runs`` — read back from the
    database here, not recomputed."""
    case_id = "MFG-2025-0734"
    case = get_case(case_id)
    assert isinstance(case, Case)
    ticket_id = seed_conversation(
        port, requester_email=case.requester_email, message=f"status of {case_id}?"
    )
    llm = FakeLLMClient(
        responses={
            Classification: Classification(
                topic="case status inquiry", route="case_status", case_id=case_id, confidence=0.97
            )
        }
    )

    run_agent(ticket_id, port=port, llm=llm)
    _, root = tracer.traces[0]

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT EXTRACT(EPOCH FROM (replied_at - received_at)) FROM runs "
            "WHERE ticket_id = %s",
            (ticket_id,),
        )
        row = cur.fetchone()
    assert row is not None
    recorded_latency = float(row[0])

    assert root.metadata["latency_s"] == pytest.approx(recorded_latency, abs=1e-6)


# --------------------------------------------------------------------------
# The times the trace reports
# --------------------------------------------------------------------------

SLOW_NODE_SECONDS = 0.6
"""Long enough to be unmistakable next to the sub-millisecond nodes around
it, short enough to cost the suite nothing. Every assertion below is stated
relative to it, so the number itself is not load-bearing."""


def _case_status_llm(case_id: str, *, classify_takes: float = 0.0) -> FakeLLMClient:
    """A fake whose `classify` model call optionally takes real time.

    The delay goes *inside the model call* — what `classify` genuinely spends
    a run doing — rather than into a substituted node, so what gets measured
    is a real node body executing real work and not a stub standing in for
    one."""

    def classification(messages: list[dict[str, Any]]) -> BaseModel:
        time.sleep(classify_takes)
        return Classification(
            topic="case status inquiry", route="case_status", case_id=case_id, confidence=0.97
        )

    return FakeLLMClient(responses={Classification: classification})


def _seed_case_status_ticket(port: EmailAdapter, case: Case) -> str:
    return seed_conversation(
        port,
        requester_email=case.requester_email,
        message=f"What is the status of case {case.case_id}?",
    )


def test_each_span_lasts_as_long_as_its_node_really_took(
    seeded: SeedResult, port: EmailAdapter, tracer: RecordingLangfuse
) -> None:
    """The defect this binds: every observation was created post-hoc in
    `act`, so a 6.5s run reported ``latency 0.002s`` with eight spans of
    0–1ms. Nothing structural could see it — the spans, names, kinds and
    nesting were all correct.

    So one node is made deliberately, measurably slow and the assertion is
    that ITS span is the slow one. Both halves matter: a fix that stamped the
    whole run's duration onto every span would satisfy "classify is slow" and
    still be a fabrication, which is what the sibling bound rules out."""
    case = get_case("MFG-2025-0734")
    assert isinstance(case, Case)
    ticket_id = _seed_case_status_ticket(port, case)
    llm = _case_status_llm(case.case_id, classify_takes=SLOW_NODE_SECONDS)

    run_agent(ticket_id, port=port, llm=llm, received_at=datetime.now(UTC))
    _, root = tracer.traces[0]

    classify = _span_named(root, "classify")
    assert classify.duration_s is not None
    assert classify.duration_s >= SLOW_NODE_SECONDS, (
        f"classify slept {SLOW_NODE_SECONDS}s inside its model call but its span "
        f"reports {classify.duration_s}s"
    )

    siblings = {c.name: c.duration_s for c in root.children if c.name != "classify"}
    assert all(d is not None for d in siblings.values()), siblings
    assert all(d < SLOW_NODE_SECONDS / 2 for d in siblings.values() if d is not None), (
        f"only classify was slow, but the other spans report {siblings} — a duration "
        "stamped uniformly across spans is not a measurement"
    )

    # The spans are laid out in real time: the pipeline is sequential, so each
    # node begins after the previous one returned.
    for earlier, later in zip(root.children, root.children[1:], strict=False):
        assert earlier.end_time_ns is not None
        assert later.start_time_ns is not None
        assert earlier.end_time_ns <= later.start_time_ns, (
            f"{earlier.name} ends after {later.name} begins"
        )


def test_the_whole_trace_spans_the_same_interval_the_run_recorded(
    seeded: SeedResult, port: EmailAdapter, tracer: RecordingLangfuse
) -> None:
    """ADR-006's trace is a demo artefact: a grader reads the latency on the
    metrics panel and then opens the trace. Those two numbers come from
    different systems and must be the same measurement, so the trace's own
    bounds are the exact ``received_at``/``replied_at`` pair in ``runs`` —
    read back out of Postgres here, not recomputed."""
    case = get_case("MFG-2025-0734")
    assert isinstance(case, Case)
    ticket_id = _seed_case_status_ticket(port, case)
    llm = _case_status_llm(case.case_id, classify_takes=SLOW_NODE_SECONDS)

    run_agent(ticket_id, port=port, llm=llm, received_at=datetime.now(UTC))
    _, root = tracer.traces[0]
    db_received, db_replied = _persisted_interval(ticket_id)

    assert root.start_time_ns == _epoch_ns(db_received)
    assert root.end_time_ns == _epoch_ns(db_replied)
    assert root.duration_s == pytest.approx((db_replied - db_received).total_seconds(), abs=1e-6)
    # And the run really was slow, so this is not two zeroes agreeing.
    assert root.duration_s is not None and root.duration_s >= SLOW_NODE_SECONDS

    # Every node's span falls inside the trace's own window.
    for child in root.children:
        assert child.start_time_ns is not None and child.end_time_ns is not None
        assert root.start_time_ns <= child.start_time_ns
        assert child.end_time_ns <= root.end_time_ns


def test_the_act_span_closes_on_the_measured_moment_the_reply_went_out(
    seeded: SeedResult, port: EmailAdapter, tracer: RecordingLangfuse
) -> None:
    """`act` is the one node whose end the clock cannot record: it emits the
    trace from inside itself, so it has not returned yet. Its span therefore
    closes on ``replied_at`` — the timestamp `act` measured when the reply
    went out and wrote to ``runs`` — and on nothing else. A ``now()`` taken
    at emission time would look identical on a fast run and drift on a slow
    one, so the assertion is equality with the persisted value."""
    case = get_case("MFG-2025-0734")
    assert isinstance(case, Case)
    ticket_id = _seed_case_status_ticket(port, case)
    llm = _case_status_llm(case.case_id)

    run_agent(ticket_id, port=port, llm=llm, received_at=datetime.now(UTC))
    _, root = tracer.traces[0]
    _, db_replied = _persisted_interval(ticket_id)

    act = _span_named(root, "act")
    assert act.end_time_ns == _epoch_ns(db_replied)
    decide = _span_named(root, "decide")
    assert decide.end_time_ns is not None
    assert act.start_time_ns is not None and act.start_time_ns >= decide.end_time_ns
    assert act.metadata is None, "act's duration is measured; nothing to caveat"


def test_a_node_nobody_timed_says_so_instead_of_reporting_zero(
    seeded: SeedResult, port: EmailAdapter, tracer: RecordingLangfuse
) -> None:
    """The clock is injected by ``run_agent``; a caller that compiles the
    graph itself brings none, and then no node's interval is known.

    An observation always has bounds, so such a span cannot be published
    "without a duration" — Langfuse will draw it as ~0ms. What it can do is
    refuse to pass off that 0 as a measurement, which is what the metadata
    marker is for. The whole-trace span still has real bounds here, because
    those come from ``received_at``/``replied_at`` and not from the clock."""
    case = get_case("MFG-2025-0734")
    assert isinstance(case, Case)
    ticket_id = _seed_case_status_ticket(port, case)
    llm = _case_status_llm(case.case_id)

    build_graph().invoke(
        {},
        config={
            "configurable": {
                "ticket_id": ticket_id,
                "deps": AgentDeps(
                    port=port, llm=llm, escalation_decider=EscalationEngine(llm=llm)
                ),
                "received_at": datetime.now(UTC),
            }
        },
    )

    _, root = tracer.traces[0]
    assert [c.name for c in root.children] == [
        "ingest",
        "classify",
        "case_status",
        "compose",
        "verify",
        "decide",
        "act",
    ]
    for child in root.children:
        assert child.start_time_ns is None, f"{child.name} claims a start nobody measured"
        assert child.end_time_ns is None, f"{child.name} claims an end nobody measured"
        assert child.metadata == {"duration": "not measured"}

    db_received, db_replied = _persisted_interval(ticket_id)
    assert root.start_time_ns == _epoch_ns(db_received)
    assert root.end_time_ns == _epoch_ns(db_replied)


def test_a_span_that_will_not_take_its_measured_start_time_says_so_out_loud(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The measured start is written through an SDK internal, because
    Langfuse's public ``start_observation`` has no ``start_time`` parameter
    at all. If a future SDK keeps that timestamp somewhere else, the write
    would land on nothing and every duration would quietly go back to being
    an artefact of when the span was built — this project's signature failure
    (an instrument that reverts to reporting something meaningless while
    still reporting confidently).

    So the write is read back, and a failure is logged. The double here is
    deliberately one whose observations have no ``_otel_span`` at all, which
    is exactly what that future SDK looks like from here."""

    class ObservationWithNowhereToPutIt:
        def __init__(self) -> None:
            self.children: list[ObservationWithNowhereToPutIt] = []

        def start_observation(self, **kwargs: Any) -> ObservationWithNowhereToPutIt:
            child = ObservationWithNowhereToPutIt()
            self.children.append(child)
            return child

        def end(self, *, end_time: int | None = None) -> None:
            return None

    class ClientWithNowhereToPutIt:
        def start_observation(self, **kwargs: Any) -> ObservationWithNowhereToPutIt:
            return ObservationWithNowhereToPutIt()

        def flush(self) -> None:
            return None

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-fake-for-tests")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-fake-for-tests")
    monkeypatch.setattr(llm_module, "_running_under_pytest", lambda: False)
    monkeypatch.setattr(llm_module, "_new_client", ClientWithNowhereToPutIt)
    llm_module.reset_tracing()

    now = datetime.now(UTC)
    with caplog.at_level("WARNING"):
        emitted = llm_module.emit_trace(
            trace_id="deadbeef" * 4,
            name="agent_run",
            spans=[llm_module.TraceSpan(name="classify", start_time=now, end_time=now)],
            start_time=now,
            end_time=now,
        )

    # Still emitted — a tripwire, not a new failure mode.
    assert emitted is True
    warned = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("'classify'" in m and "not a measurement" in m for m in warned), warned
    assert any("'agent_run'" in m for m in warned), warned


def _exploding_import() -> Any:
    raise ImportError("No module named 'langfuse'")


@pytest.mark.parametrize(
    ("label", "client_factory"),
    [
        ("the SDK rejects the spans", lambda: RecordingLangfuse(fail=True)),
        ("the package will not even import", _exploding_import),
    ],
    ids=["span-emission-fails", "client-construction-fails"],
)
def test_a_broken_tracer_never_costs_the_customer_a_reply(
    seeded: SeedResult,
    port: EmailAdapter,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    client_factory: Any,
) -> None:
    """A trace is diagnostics; the reply is the product. This is the whole
    justification for `emit_trace`'s bare ``except`` — without it, a Langfuse
    outage in the middle of a demo would raise out of `act`, `worker.main`
    would release the dedup row and log an ERROR, and the customer would get
    nothing even though the reply had already been posted.

    Both failure points are parametrized because they are genuinely
    different code, and the second one was a live defect: the client is
    built by ``from langfuse import Langfuse`` inside `_new_client`, which
    for a while ran OUTSIDE the guard. A tripwire run with the package
    import blocked — a broken wheel, a version conflict, an image built
    without the dependency — raised ``ImportError`` straight out of `act`
    after the reply had already gone out."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-fake-for-tests")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-fake-for-tests")
    monkeypatch.setattr(llm_module, "_running_under_pytest", lambda: False)
    monkeypatch.setattr(llm_module, "_new_client", client_factory)
    llm_module.reset_tracing()

    case_id = "MFG-2025-0734"
    case = get_case(case_id)
    assert isinstance(case, Case)
    ticket_id = seed_conversation(
        port, requester_email=case.requester_email, message=f"status of {case_id}?"
    )
    llm = FakeLLMClient(
        responses={
            Classification: Classification(
                topic="case status inquiry", route="case_status", case_id=case_id, confidence=0.97
            )
        }
    )

    result = run_agent(ticket_id, port=port, llm=llm)

    assert result["route"] == "case_status"
    assert len(port.transport.sent) == 1
    assert case_id in port.transport.sent[0].html_body
    # And the run was still recorded, with its (now untraceable) id.
    assert _persisted_trace_id(ticket_id)
