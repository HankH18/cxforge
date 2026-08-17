"""W2-C1 (ADR-006) — the Langfuse trace `act` emits for every run.

`docs/STATE.md §6.3`: Langfuse was a declared dependency with zero
``import langfuse`` repo-wide. `act` minted a ``trace_id``, persisted it on
``runs.trace_id``, and told nobody — so every trace link the portal built
pointed at a trace that did not exist.

Two properties carry this feature, and each is tested on its own here
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

The double below records what the SDK would have been asked to do. It is
not a Langfuse stand-in and does not pretend to be one: whether Langfuse
accepts these calls is proven by reading a real trace back from the API
(W2-C1's report), which no test can do offline. What it does prove is the
half a live check cannot — that the ids line up, that a span exists for
every node that ran and none for a node that did not, and that the
`compose` span carries the exact `Case` its reply was rendered from.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent import llm as llm_module
from agent.graph import run_agent
from agent.schemas import Classification, GroundednessJudgment, KBAnswerDraft
from data import Case, get_case, get_connection
from data.seed import SeedResult
from helpdesk.email_adapter import EmailAdapter

from .conftest import seed_conversation, set_gate
from .fakes import FakeLLMClient

# --------------------------------------------------------------------------
# The recording double
# --------------------------------------------------------------------------


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

    def end(self) -> None:
        self.ended = True


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
