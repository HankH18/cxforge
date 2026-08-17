"""W1-E1 acceptance — the promptfoo suite stays bound to the shipped prompts.

``docs/STATE.md §6.11`` recorded ``promptfooconfig.yaml`` as a T-1 scaffold:
placeholder prompt, ``tests: []``. ADR-013 replaced it. The failure mode this
module guards is the *easy* regression, not the obvious one — nobody will put
``tests: []`` back, but it is very easy to "simplify" the custom provider into
``anthropic:messages:claude-opus-5`` with the system prompt pasted into YAML.
That version still runs, still shows green, and measures the YAML instead of
``backend/src/agent/prompts.py`` — evidence-shaped and worthless.

So these tests are offline and structural. They never call a model and never run
promptfoo; the live behaviour (and the sabotage evidence that the suite goes red
when a prompt is degraded) is recorded in ``promptfooconfig.yaml``'s own header.
No new first-party import root is added to this directory — see
``test_route_accuracy.py``'s docstring for why that matters.

WHAT THIS MODULE CANNOT SEE, AND WHERE THAT IS COVERED
------------------------------------------------------
Structural is not the same as executed. Nothing here runs
``evals/promptfoo/provider.py``, so when W2-B4/ADR-009 gave ``classify`` a
``port.fetch_requester_history`` call the provider could not answer, every
classify case in the suite errored for a whole wave (5 passed, 19 errors) and
all six tests below stayed green. ``test_promptfoo_provider.py`` closes that: it
calls the real ``call_api`` over the real ``classify`` and ``compose`` nodes,
offline, and is the module that goes red when a node grows a dependency the
provider does not supply. The two are complements — the checks below are what
stop the suite being "simplified" into a raw provider with a pasted prompt, and
they must not be weakened to make anything pass.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = REPO_ROOT / "promptfooconfig.yaml"
PROMPTFOO_DIR = REPO_ROOT / "evals" / "promptfoo"
PROMPTS_MODULE = REPO_ROOT / "backend" / "src" / "agent" / "prompts.py"


@pytest.fixture(scope="module")
def config() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(CONFIG_PATH.read_text())
    return loaded


def test_the_scaffold_is_gone(config: dict[str, Any]) -> None:
    """T-1's placeholder prompt and empty test list must not come back."""
    assert config["tests"], "promptfooconfig.yaml has no tests — this is the T-1 scaffold again"
    rendered = yaml.safe_dump(config)
    assert "Placeholder" not in rendered
    assert "T-7 supplies" not in rendered


def test_the_provider_is_the_shipped_agent_and_not_a_raw_model(config: dict[str, Any]) -> None:
    """The whole point of E1: measure the code that ships.

    A raw ``anthropic:messages:*`` provider would require the prompt to live in
    this YAML, and editing ``agent/prompts.py`` would then leave the suite green.
    """
    providers = config["providers"]
    assert len(providers) == 1, "one provider — the shipped agent nodes"
    provider_id = providers[0]["id"] if isinstance(providers[0], dict) else providers[0]
    assert provider_id == "file://evals/promptfoo/provider.py"
    assert not provider_id.startswith("anthropic:"), (
        "a raw model provider means the prompt under test lives in YAML, not in "
        "backend/src/agent/prompts.py — the suite would stay green through any "
        "prompt degradation"
    )


def test_no_prompt_text_is_duplicated_into_the_config(config: dict[str, Any]) -> None:
    """No copy of a shipped system prompt anywhere in the promptfoo tree.

    Checked against the real strings in ``agent/prompts.py`` rather than a
    keyword list, so it keeps working when those prompts are reworded.
    """
    source = PROMPTS_MODULE.read_text()
    # A distinctive interior fragment of each shipped system prompt: long enough
    # that an accidental match is implausible, short enough to survive the
    # source's own line wrapping.
    fragments = [
        "classify the customer's latest ",
        "closed list of always-granted request kinds",
        "using ONLY the knowledge-",
        "strict groundedness judge",
    ]
    for fragment in fragments:
        assert fragment in source, f"{fragment!r} is no longer in agent/prompts.py — update this"

    # Scan the PARSED yaml, not the raw bytes: comments never reach promptfoo,
    # and promptfooconfig.yaml's own header deliberately quotes fragments of the
    # shipped prompts while documenting the sabotage evidence. What matters is
    # whether a prompt is present as a *value* the suite would send.
    haystack = yaml.safe_dump(config)
    for path in sorted(PROMPTFOO_DIR.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix in {".yaml", ".yml"}:
            haystack += yaml.safe_dump(yaml.safe_load(path.read_text()))
        elif path.suffix in {".txt", ".json"}:
            haystack += path.read_text()
    for fragment in fragments:
        assert fragment not in haystack, (
            f"a shipped system prompt is duplicated into the promptfoo tree ({fragment!r}). "
            "The suite must read the prompt from backend/src/agent/prompts.py, or it stops "
            "detecting prompt degradation."
        )


def test_every_referenced_file_exists(config: dict[str, Any]) -> None:
    referenced = [config["providers"][0]["id"], *config["tests"]]
    for entry in referenced:
        assert entry.startswith("file://"), entry
        target = REPO_ROOT / entry.removeprefix("file://").split(":", 1)[0]
        assert target.exists(), f"promptfooconfig.yaml references a missing file: {target}"


def test_grounding_cases_declare_a_guard_expectation_and_a_content_assertion() -> None:
    """``expect_guard`` must never be the only thing a case asserts.

    A case that pins only the guard verdict would still pass if the model
    started fabricating in a way that happens to trip the same guard rule —
    a vacuous green. Every grounding case therefore also carries a content
    assertion (``icontains-any`` / ``not-icontains``) about what it must say.
    """
    cases = yaml.safe_load((PROMPTFOO_DIR / "tests_grounding.yaml").read_text())
    assert cases, "the adversarial grounding set is empty"

    for case in cases:
        description = case["description"]
        assert case["vars"]["suite"] == "kb_answer", description
        assert case["vars"]["expect_guard"] in ("clean", "violation"), description

        assertion_types = {a["type"] for a in case["assert"]}
        assert "python" in assertion_types, f"{description}: no shipped-guard assertion"
        content_assertions = assertion_types & {
            "icontains-any",
            "contains-any",
            "not-icontains",
            "not-contains",
        }
        assert content_assertions, (
            f"{description}: pins the guard verdict but asserts nothing about what the "
            "answer says — that is a vacuous green waiting to happen"
        )


def test_grounding_cases_name_real_kb_fixtures() -> None:
    """A typo'd slug would silently mean an empty KB context, and every
    grounding assertion would then pass on an answer grounded in nothing."""
    kb_dir = REPO_ROOT / "fixtures" / "kb"
    available = {path.stem for path in kb_dir.glob("*.md")}
    cases = yaml.safe_load((PROMPTFOO_DIR / "tests_grounding.yaml").read_text())
    for case in cases:
        slugs = [s.strip() for s in str(case["vars"]["kb_docs"]).split(",") if s.strip()]
        assert slugs, case["description"]
        missing = set(slugs) - available
        assert not missing, f"{case['description']}: unknown kb docs {sorted(missing)}"
