"""W1-E1 acceptance, executing half — ``provider.call_api`` is actually RUN.

``test_promptfoo_suite.py`` next door validates the *shape* of
``promptfooconfig.yaml``: that the provider is the custom Python one and not a
raw ``anthropic:messages:*`` with a pasted prompt, that the cases are generated
from the approved labeled set, that no grounding case pins only a guard verdict.
Every one of those checks is load-bearing and none of them executes a single
line of ``evals/promptfoo/provider.py``.

THE REGRESSION THIS MODULE EXISTS FOR
-------------------------------------
W2-B4 / ADR-009 gave ``agent.nodes.classify`` a call to
``deps.port.fetch_requester_history``. ``evals/promptfoo/provider.py`` was still
filling the ``port`` slot of ``AgentDeps`` with ``_Unused("port")`` — a sentinel
that raises ``AssertionError`` on any attribute access. So from ``cabefd8``
onward every classify case in the promptfoo suite errored (measured: 5 passed,
19 errors; after the fix, 24 passed, 0 errors), and the whole "second,
independent evidence stream" of ADR-013 was dead for an entire wave while the
gated suite stayed green — 810 tests, none of which called ``call_api``.

This is the project's signature failure, third instance (``docs/STATE.md``
§6.1-6.2: 702 green tests over a severed core loop; a droplet passing
``verify_deploy.sh`` 4/4 with no ``ANTHROPIC_API_KEY``). Green proved the
components worked and proved nothing about the system.

So: these tests call the REAL ``call_api`` against the REAL ``classify`` and
``compose`` nodes. **The specific failure mode they defend against is a
collaborator the node needs that the provider does not supply** — a sentinel
that raises on access, or a port missing a method. That failure surfaces here as
``call_api`` returning ``{"error": ...}``, which is exactly what promptfoo
reports as a case error.

NO LIVE CALLS
-------------
The model is replaced by ``provider._CannedLLMClient`` through
``EVALS_PROMPTFOO_FAKE_LLM_FOR_TESTS_ONLY``, the provider's TEST-ONLY hatch,
gated on TWO independent signals exactly as ``evals/route_accuracy.py``'s is.
``test_fake_llm_hatch_is_refused_outside_a_pytest_process`` is the structural
proof it cannot leak into a real promptfoo eval. Nothing here touches the
network, needs an API key, or needs the database.

The nodes are NOT faked, the deps are NOT faked, and ``call_api`` is not
re-implemented — only the model is canned, and the canned client matches on the
text it was handed, so the tests can also assert that the shipped
``CLASSIFY_SYSTEM`` / ``KB_ANSWER_SYSTEM`` and the promptfoo case vars really
reached the model call site.

WHY THE PROVIDER IS LOADED BY PATH
----------------------------------
``evals/promptfoo/`` has no ``__init__.py`` and is not a package: promptfoo
loads ``provider.py`` with importlib under a synthetic module name. Doing the
same here keeps this directory's first-party import roots unchanged (see
``test_route_accuracy.py``'s docstring) and exercises the module the same way
promptfoo does, including its ``sys.path`` bootstrap.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from agent.prompts import CLASSIFY_SYSTEM, KB_ANSWER_SYSTEM

REPO_ROOT = Path(__file__).resolve().parents[3]
PROVIDER_PATH = REPO_ROOT / "evals" / "promptfoo" / "provider.py"
FAKE_LLM_ENV_VAR = "EVALS_PROMPTFOO_FAKE_LLM_FOR_TESTS_ONLY"
REEXEC_GUARD_ENV_VAR = "CXFORGE_PROMPTFOO_REEXEC"

# A real KB fixture, used both as `compose`'s context and as the needle that
# proves the fixture text actually reached the model call.
KB_SLUG = "turnaround-times"
KB_TEXT = (REPO_ROOT / "fixtures" / "kb" / f"{KB_SLUG}.md").read_text()

# A string that appears in no prompt and no fixture, so a match on it can only
# mean the promptfoo `message` var reached the node.
MESSAGE_MARKER = "zq-promptfoo-provider-marker-7719"

# The suite's own size, pinned so ``promptfooconfig.yaml``'s "24 of 24" baseline
# cannot go stale unnoticed again — that header sat at "23 of 25" through
# ADR-020's relabel. 19 generated classify cases (3 per canonical route + the 7
# route-dependent escalations ``test_route_accuracy.py`` pins) plus the 5
# adversarial grounding cases. If evals/labeled_set.yaml moves, these must be
# re-derived deliberately AND the header re-measured, not silently absorbed.
EXPECTED_CLASSIFY_CASES = 19
EXPECTED_GROUNDING_CASES = 5
EXPECTED_SUITE_CASES = 24


def _classification(route: str, **overrides: Any) -> dict[str, Any]:
    payload = {"topic": "canned topic", "route": route, "case_id": None, "confidence": 0.5}
    payload.update(overrides)
    return payload


def _hatch(spec: dict[str, Any]) -> str:
    return json.dumps(spec)


def _load(name: str) -> ModuleType:
    """Load one of ``evals/promptfoo/*.py`` the way promptfoo does.

    ``CXFORGE_PROMPTFOO_REEXEC`` is set first and deliberately: ``_bootstrap``
    re-execs the interpreter with ``os.execv`` when it is not already the repo's
    venv python, and doing that from inside a pytest process would replace the
    test run itself. Under ``uv run pytest`` the guard is a no-op (same
    interpreter); on any other invocation it is the difference between a clear
    result and a silently restarted process.
    """
    os.environ.setdefault(REEXEC_GUARD_ENV_VAR, "1")
    path = REPO_ROOT / "evals" / "promptfoo" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"cxforge_promptfoo_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def provider() -> ModuleType:
    return _load("provider")


def _call(
    provider: ModuleType, monkeypatch: pytest.MonkeyPatch, spec: dict[str, Any], **variables: Any
) -> dict[str, Any]:
    monkeypatch.setenv(FAKE_LLM_ENV_VAR, _hatch(spec))
    result: dict[str, Any] = provider.call_api("", {}, {"vars": variables})
    return result


def _assert_no_error(result: dict[str, Any], node: str) -> None:
    assert "error" not in result, (
        f"provider.call_api could not drive the shipped agent.nodes.{node}: "
        f"{result.get('error')!r}.\n"
        "Every promptfoo case on this suite errors like this — the whole ADR-013 evidence "
        "stream is dead.\n"
        "The overwhelmingly likely cause is a COLLABORATOR THE NODE NEEDS THAT "
        "evals/promptfoo/provider.py DOES NOT SUPPLY: an AgentDeps slot still holding an "
        "_Unused sentinel (which raises AssertionError on any attribute access), or a port "
        "stub missing a method the node now calls. That is exactly how W2-B4/ADR-009 killed "
        "all 19 classify cases while 810 offline tests stayed green.\n"
        "Fix the provider's deps — do NOT relax this assertion."
    )


# ---------------------------------------------------------------------------
# The load-bearing tests: call_api, the real nodes, the real deps
# ---------------------------------------------------------------------------


def test_call_api_drives_the_shipped_classify_node(
    provider: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The test that would have gone red the moment ADR-009 landed.

    ``classify`` reaches ``deps.port.fetch_requester_history`` before it reaches
    ``deps.llm``, so a port the provider cannot satisfy fails here regardless of
    what the model would have said.
    """
    result = _call(
        provider,
        monkeypatch,
        {"Classification": {"default": _classification("permission", case_id="CASE-4242")}},
        suite="classify",
        message="Please add my sister as an authorized contact on my case.",
    )
    _assert_no_error(result, "classify")

    payload = json.loads(result["output"])
    # Every field comes back through agent.nodes.classify's own return shape —
    # `case_id` in particular is not passed through, it is the node lifting
    # Classification.case_id into tool_results["case_id_hint"].
    assert payload["route"] == "permission"
    assert payload["topic"] == "canned topic"
    assert payload["confidence"] == 0.5
    assert payload["case_id"] == "CASE-4242"


def test_the_classify_call_carries_the_shipped_prompt_and_the_promptfoo_message(
    provider: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not merely "a model call happened" — the RIGHT text reached it.

    The canned client matches on the assembled messages, so a needle that hits
    proves that string was in the payload the shipped node built. Both needles
    are computed, not copied: ``CLASSIFY_SYSTEM`` is imported from
    ``agent.prompts``, so this keeps working when the prompt is reworded and
    goes red if the node stops sending it.
    """
    spec = {
        "Classification": {
            "default": _classification("off_topic"),
            "matches": [[CLASSIFY_SYSTEM, _classification("kb")]],
        }
    }
    result = _call(
        provider, monkeypatch, spec, suite="classify", message="How long does a rush take?"
    )
    _assert_no_error(result, "classify")
    assert json.loads(result["output"])["route"] == "kb", (
        "agent.prompts.CLASSIFY_SYSTEM was not in the messages agent.nodes.classify sent. "
        "The promptfoo suite would then be measuring some other prompt — the exact thing "
        "test_promptfoo_suite.py's structural checks exist to prevent, now checked at runtime."
    )

    spec["Classification"]["matches"] = [[MESSAGE_MARKER, _classification("case_status")]]
    result = _call(
        provider,
        monkeypatch,
        spec,
        suite="classify",
        message=f"Any update on my case? {MESSAGE_MARKER}",
    )
    _assert_no_error(result, "classify")
    assert json.loads(result["output"])["route"] == "case_status", (
        "the promptfoo case's `message` var never reached the classifier — the suite would "
        "score every case against the same input"
    )


def test_call_api_drives_the_shipped_compose_node(
    provider: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``kb_answer`` half, and the same dependency guarantee.

    ``compose``'s ``"kb"`` branch only reaches ``deps.llm`` today. Running it
    here means the day it grows a port or escalation-decider call, this goes red
    instead of the promptfoo suite quietly erroring on every grounding case.
    """
    result = _call(
        provider,
        monkeypatch,
        {"KBAnswerDraft": {"default": {"answer": "canned draft"}}},
        suite="kb_answer",
        message="What is the usual turnaround?",
        kb_docs=KB_SLUG,
        topic="turnaround",
    )
    _assert_no_error(result, "compose")

    payload = json.loads(result["output"])
    assert payload["draft"] == "canned draft"
    assert payload["kb_docs"] == [KB_SLUG]


def test_the_compose_call_carries_the_shipped_prompt_and_the_real_kb_fixture(
    provider: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``fixtures/kb/*.md`` text must actually reach the model.

    An empty or lost KB context would make every grounding assertion in
    ``tests_grounding.yaml`` trivially satisfiable, which is the vacuous green
    ``assert_grounded.py``'s docstring is about.
    """
    spec = {
        "KBAnswerDraft": {
            "default": {"answer": "MISS"},
            "matches": [[KB_ANSWER_SYSTEM, {"answer": "PROMPT-REACHED-THE-MODEL"}]],
        }
    }
    result = _call(
        provider,
        monkeypatch,
        spec,
        suite="kb_answer",
        message="What is the usual turnaround?",
        kb_docs=KB_SLUG,
        topic="turnaround",
    )
    _assert_no_error(result, "compose")
    assert json.loads(result["output"])["draft"] == "PROMPT-REACHED-THE-MODEL", (
        "agent.prompts.KB_ANSWER_SYSTEM was not in the messages agent.nodes.compose sent"
    )

    spec["KBAnswerDraft"]["matches"] = [[KB_TEXT[:200], {"answer": "KB-REACHED-THE-MODEL"}]]
    result = _call(
        provider,
        monkeypatch,
        spec,
        suite="kb_answer",
        message="What is the usual turnaround?",
        kb_docs=KB_SLUG,
        topic="turnaround",
    )
    _assert_no_error(result, "compose")
    assert json.loads(result["output"])["draft"] == "KB-REACHED-THE-MODEL", (
        f"the text of fixtures/kb/{KB_SLUG}.md never reached compose's context — every "
        "grounding assertion in the promptfoo suite would then be grading an answer "
        "composed over silence"
    )


def test_the_provider_deps_are_narrow_and_not_a_blanket_stub(
    provider: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the contract, and the tempting wrong fix.

    The way to make the tests above pass forever is to hand ``AgentDeps`` a
    permissive mock. That would silently turn the eval into a measurement of
    something the product never does. So: the port answers exactly the one
    method ``classify`` needs and raises on everything else, and
    ``escalation_decider`` raises on any access at all — the loud-failure
    property ``provider.py``'s docstring claims, asserted rather than trusted.
    """
    spec = {"Classification": {"default": _classification("kb")}}
    monkeypatch.setenv(FAKE_LLM_ENV_VAR, _hatch(spec))
    deps = provider._config("promptfoo-case")["configurable"]["deps"]

    assert deps.port.fetch_requester_history("a@b.invalid", exclude_ticket_id="x") == []
    for attribute in ("fetch_ticket", "post_public_reply", "add_tags", "set_status"):
        with pytest.raises(AssertionError, match="must not touch deps.port"):
            getattr(deps.port, attribute)
    with pytest.raises(AssertionError, match="must not touch deps.escalation_decider"):
        getattr(deps.escalation_decider, "decide")  # noqa: B009 - the ACCESS is what raises


def test_an_unknown_suite_is_an_error_and_never_a_silent_pass(
    provider: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A case that forgets ``suite:`` must not fall through to a default node."""
    result = _call(
        provider,
        monkeypatch,
        {"Classification": {"default": _classification("kb")}},
        message="no suite declared",
    )
    assert "error" in result and "unknown suite" in result["error"]


# ---------------------------------------------------------------------------
# The suite's other two executable pieces — same blind spot, same fix
# ---------------------------------------------------------------------------


def test_the_grounding_assertion_runs_the_shipped_guard() -> None:
    """``assert_grounded.get_assert`` is the R9 verdict for 5 of the 24 cases.

    It was in exactly the same position ``provider.call_api`` was:
    ``test_promptfoo_suite.py`` asserts that every grounding case declares a
    ``python`` assertion pointing at this file, and nothing ever called it. If it
    stopped agreeing with ``agent.grounding_guard`` — or just stopped importing —
    those 5 cases would error and the offline suite would not notice.
    """
    assert_grounded = _load("assert_grounded")
    fabricated = json.dumps(
        {"draft": "Your case MFG-0000-0000 is at the genealogy stage, done in 3 weeks."}
    )
    clean = json.dumps(
        {
            "draft": (
                "Most cases take 3-5 weeks per the published turnaround windows. "
                "I do not have access to individual case records."
            )
        }
    )

    # The verdict is the shipped guard's, and it moves in BOTH directions.
    drifted = (
        "evals/promptfoo/assert_grounded.py is no longer returning "
        "agent.grounding_guard.find_ungrounded_case_claims' verdict. The R9 assertion on 5 of "
        "the 24 promptfoo cases is then a second opinion that can drift from the guard the "
        "product actually escalates on — the exact thing that file's docstring promises it is "
        "not."
    )
    flagged = assert_grounded.get_assert(fabricated, {"vars": {"expect_guard": "clean"}})
    assert flagged["pass"] is False, f"a fabricated case fact passed as clean. {drifted}"
    assert "R9 REGRESSION" in flagged["reason"], flagged
    assert assert_grounded.get_assert(fabricated, {"vars": {"expect_guard": "violation"}})["pass"]
    assert assert_grounded.get_assert(clean, {"vars": {"expect_guard": "clean"}})["pass"], drifted

    stale = assert_grounded.get_assert(clean, {"vars": {"expect_guard": "violation"}})
    assert stale["pass"] is False, "a declared `violation` case stopped tripping the guard"
    assert "PINNED EXPECTATION IS STALE" in stale["reason"], stale

    # A provider that errored returns no draft — that must fail, never pass.
    broken = assert_grounded.get_assert("not json", {"vars": {"expect_guard": "clean"}})
    assert broken["pass"] is False, (
        "a case whose provider ERRORED was graded as passing — that is how a dead evidence "
        "stream reports green"
    )
    assert "did not return a kb draft" in broken["reason"], broken


def test_the_case_generator_still_builds_the_suite_the_header_claims() -> None:
    """``gen_tests.generate_classify_tests`` builds 19 of the 24 cases at eval
    time, from ``evals/labeled_set.yaml``. Never executed offline either — a
    labeled-set shape change would surface only as a broken ``npx promptfoo
    eval``, and the header's "24 of 24" would silently describe a different
    suite. Pinning the count here is what makes that number re-checkable.
    """
    gen_tests = _load("gen_tests")
    cases = gen_tests.generate_classify_tests()
    assert len(cases) == EXPECTED_CLASSIFY_CASES, (
        f"the generated classify suite is {len(cases)} cases, not {EXPECTED_CLASSIFY_CASES}. "
        "promptfooconfig.yaml's MEASURED BASELINE describes a suite of "
        f"{EXPECTED_SUITE_CASES} — re-measure it and update the header, do not just move "
        "this number."
    )

    grounding = yaml.safe_load(
        (REPO_ROOT / "evals" / "promptfoo" / "tests_grounding.yaml").read_text()
    )
    assert len(grounding) == EXPECTED_GROUNDING_CASES
    assert len(cases) + len(grounding) == EXPECTED_SUITE_CASES

    for case in cases:
        assert case["vars"]["suite"] == "classify", case["description"]
        assert case["vars"]["message"].strip(), case["description"]
        # The route assertion is a javascript EXPRESSION over the provider's
        # JSON output — the shape promptfoo 0.122.0 actually accepts.
        types = {assertion["type"] for assertion in case["assert"]}
        assert {"is-json", "javascript"} <= types, case["description"]


# ---------------------------------------------------------------------------
# The hatch cannot leak into a real promptfoo eval
# ---------------------------------------------------------------------------


_GATE_PROBE = """
import importlib.util, json, sys

spec = importlib.util.spec_from_file_location("gate_probe_provider", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(json.dumps(module.call_api("", {}, {"vars": {"suite": "classify", "message": "hi"}})))
"""


def _probe(tmp_path: Path, *, keep_pytest_marker: bool) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "gate_probe.py"
    script.write_text(_GATE_PROBE)
    env = dict(os.environ)
    env[FAKE_LLM_ENV_VAR] = _hatch({"Classification": {"default": _classification("kb")}})
    env[REEXEC_GUARD_ENV_VAR] = "1"
    # If the gate ever fails open, these make the fallback path stop at "no key"
    # instead of spending money on a live call from inside the offline suite.
    # `load_dotenv(override=False)` will not overwrite a key already present, so
    # an empty string is what the provider sees.
    env["ANTHROPIC_API_KEY"] = ""
    env["HOME"] = str(tmp_path)
    if not keep_pytest_marker:
        # PYTEST_VERSION is the second, independent signal. Removing it is what
        # a plain `npx promptfoo eval` shell looks like.
        env.pop("PYTEST_VERSION", None)
        env.pop("PYTEST_CURRENT_TEST", None)
    else:
        env.setdefault("PYTEST_VERSION", "8.0.0")
    return subprocess.run(
        [sys.executable, str(script), str(PROVIDER_PATH)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def test_fake_llm_hatch_is_refused_outside_a_pytest_process(tmp_path: Path) -> None:
    """The canned-response hatch must need TWO signals, not one.

    Gated on the env var alone it would be convention, not structure: a leaked
    export would turn a real promptfoo eval into a suite that never reached the
    model and still printed pass/fail. Same defect, same fix, and the same proof
    as ``test_route_accuracy.py``'s equivalent.

    Run as a paired probe — identical in every respect except the marker — so a
    red here can only mean the marker, not something incidental about running
    the provider in a subprocess.
    """
    refused = _probe(tmp_path, keep_pytest_marker=True)
    assert refused.returncode == 0, refused.stderr
    honoured = json.loads(refused.stdout)
    assert "error" not in honoured, honoured
    assert json.loads(honoured["output"])["route"] == "kb"

    leaked = _probe(tmp_path, keep_pytest_marker=False)
    assert leaked.returncode == 0, leaked.stderr
    payload = json.loads(leaked.stdout)
    assert "not a pytest process" in payload.get("error", ""), payload
    assert "ANTHROPIC_API_KEY" not in payload.get("error", ""), (
        "the hatch was ignored and the provider fell through to the real client — the gate "
        "must refuse BEFORE it reaches the model path"
    )
