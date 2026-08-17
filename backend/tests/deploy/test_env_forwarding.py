"""W1-F3 — the env-forwarding audit.

`docs/STATE.md §6.2`: the stack passed its deploy check 4/4 for weeks with
**no `ANTHROPIC_API_KEY` at all**, because `deploy/docker-compose.yml`
forwarded `OPENAI_API_KEY` (the pre-pivot name) and nothing anywhere compared
what the application *reads* against what the containers *get*. Every deploy
check in this repo is a liveness check; none of them would have noticed.

This module is the missing comparison. The variable list is **derived, not
written down** wherever a machine can derive it — and where it provably
cannot, the gap is a *required* declaration rather than a silent hole. Three
rules, computed on every run:

  1. **AST** over ``backend/src/**`` and ``deploy/backend/bootstrap.py`` —
     every ``os.environ[...]``, ``os.environ.get(...)``, ``os.getenv(...)``
     and ``os.environ.setdefault(...)`` whose key is a string literal.
  2. **Text** — names declared in ``.env.example`` that appear anywhere in
     that same source.
  3. **SDK_RESOLVED_VARIABLES** — credentials a third-party client reads out
     of the environment by itself, where our source may not contain the name
     at all.

Rule 3 is here because rules 1 and 2 were *both* measured blind to a live
example. `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` are resolved by the
`langfuse` SDK, so there is no ``os.environ`` read for rule 1, and neither
name appears anywhere in ``backend/src/**`` (only ``LANGFUSE_HOST`` does), so
rule 2 sees nothing either. Verified 2026-08-16 by deleting all six
credential lines from both compose files: the suite stayed **45 passed**. The
`langfuse` instrumentation W2-C1 is about to add would then have landed on a
worker with no keys — `docs/STATE.md §6.2` reproduced exactly.

`ANTHROPIC_API_KEY` is the same shape and got caught only by luck: it happens
to be *named* in `agent/llm.py`'s docstring, which is not a property anyone
guaranteed. It is in rule 3 now too, so it no longer depends on a comment
surviving.

**Rule 3 is required, never exempting.** Deleting a line from it is how a
credential stops being checked, so the only way to add an SDK credential
safely is to add a line — the opposite of the historical failure, where
forgetting was free.

Deliberate non-goals and known blind spots, so nobody mistakes a pass here for
more than it is:

* This proves a variable is **forwarded**, not that it is **set**. A
  ``${ANTHROPIC_API_KEY:-}`` that renders to an empty string passes — the
  variable reaches the container, empty. Catching *that* needs a run that
  makes a real model call, which is W3-G2's deep deploy check, not a test.
* ``scripts/**`` and ``evals/**`` are **not** scanned for forwarding, on
  purpose: nothing runs them in a container. They are checked only for
  ``.env.example`` completeness, below.
* A `pydantic-settings` ``BaseSettings`` subclass declares env vars as
  lowercase class attributes and would be invisible to rule 1. There is no
  such class in the tree today (checked 2026-08-16); if one appears, rule 1
  needs a fourth clause.
* It parses YAML. It does not start a container.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]

# The application, as it is imported inside the container image.
APP_SOURCE_ROOT = REPO_ROOT / "backend" / "src"

# Runs only under the image's ENTRYPOINT (deploy/backend/entrypoint.sh), so
# its variables are required only on services that keep that entrypoint. That
# distinction is derived from each service's `entrypoint:` key below, not
# asserted here.
ENTRYPOINT_SOURCES = (REPO_ROOT / "deploy" / "backend" / "bootstrap.py",)

ENV_EXAMPLE = REPO_ROOT / ".env.example"

COMPOSE_FILES = (
    REPO_ROOT / "docker-compose.yml",
    REPO_ROOT / "deploy" / "docker-compose.yml",
)

# The application image. A service that builds from this Dockerfile runs the
# application code, whatever command it is given — which is exactly why
# `worker` needs the same environment as `backend` (ADR-002: same image,
# different command).
APP_DOCKERFILE = "deploy/Dockerfile.backend"

# Reserved for the day images are built in CI and the droplet pulls instead of
# building. Nothing uses it yet; it exists so that switching to a prebuilt
# image is a one-line change here rather than a silent loss of coverage.
APP_IMAGE_PREFIX = "cxforge/backend"

# What starting the application looks like from the outside, whatever the
# image came from: uvicorn serving `main:app`, or arq running the worker.
APP_ENTRY_MARKERS = ("uvicorn", "arq ", "arq worker", "main:app")

# Env reads whose key is not a string literal, so no static pass can resolve
# them. This is a ledger of what the scan CANNOT see, keyed by module and
# recorded as source text (not line numbers, which shift under unrelated
# edits). It is not a list of variables and it is not an exemption: a new
# dynamic read fails this module and forces someone to look at it, which is
# the opposite of the silent under-reporting that this suite exists to stop.
KNOWN_DYNAMIC_ENV_READS: dict[str, set[str]] = {
    # T-16/T-24 per-process test-schema isolation. The name resolves to
    # OTHRAM_TEST_SCHEMA, a pytest-only variable that must NOT be forwarded
    # into any container — see backend/src/data/db.py's module docstring.
    "backend/src/data/db.py": {"os.environ.get(TEST_SCHEMA_ENV_VAR)"},
}

# Rule 3. Credentials a third-party SDK resolves from the environment on its
# own, so neither the AST scan nor the text scan can be relied on to find
# them. Every entry is REQUIRED in every application container; the value is
# the evidence that it belongs here.
#
# Adding an SDK that reads its own credential means adding a line. Deleting a
# line means that credential stops being checked, which is the failure mode
# `docs/STATE.md §6.2` describes — so a deletion should be as deliberate as
# removing the dependency.
SDK_RESOLVED_VARIABLES: dict[str, str] = {
    "ANTHROPIC_API_KEY": (
        "backend/src/agent/llm.py builds anthropic.Anthropic(api_key=None) and "
        "lets the SDK resolve it. The name survives in that file only as a "
        "docstring, which is not a guarantee — this line is."
    ),
    "LANGFUSE_PUBLIC_KEY": (
        "Resolved by the langfuse SDK (ADR-006 / W2-C1). The name appears "
        "nowhere in backend/src/**; only LANGFUSE_HOST does."
    ),
    "LANGFUSE_SECRET_KEY": (
        "Resolved by the langfuse SDK (ADR-006 / W2-C1). In Langfuse the key "
        "PAIR is the project pointer (docs/STATE.md §3.1), so a worker with "
        "one and not the other traces to the wrong project or nowhere."
    ),
    "VOYAGE_API_KEY": (
        "Resolved by the voyageai SDK. ADR-008 / W2-B1 replaces HashingEmbedder "
        "with VoyageEmbedder; retrieval runs inside the worker, so the worker "
        "needs it and not only the seeder."
    ),
}

# Rule 2 (the text scan) cannot tell an implicit read from a cross-reference:
# `ANTHROPIC_API_KEY` appears in `agent/llm.py` because the SDK resolves it,
# and `DEPLOY_HOST` appears in `main.py` because a docstring cites its
# precedence rule. Nothing textual separates them, so this is where that
# decision is recorded.
#
# THIS IS THE INVERSE OF THE CHECKLIST IT REPLACES, and the direction is the
# whole point. A hand-maintained list of variables *to forward* fails open:
# forget an entry and nothing complains, which is exactly how the stack ran
# for weeks with no `ANTHROPIC_API_KEY`. This list fails closed: a declared
# variable named anywhere in `backend/src/**` is required in every container
# **unless** it appears here with a reason. Forgetting an entry turns the
# suite red.
#
# It can only ever exempt a *mention*. A variable this code actually reads
# (`os.environ`/`os.getenv`) can never be exempted — asserted below.
NOT_CONTAINER_VARIABLES: dict[str, str] = {
    "DEPLOY_HOST": (
        "Names the droplet for scripts/verify_deploy.sh, which runs on the "
        "operator's machine, not in the stack. No process inside any "
        "container reads it. Mentioned in backend/src/main.py only as the "
        "precedent for load_repo_dotenv's override=False precedence."
    ),
}

# An env var name as .env / compose write it: SCREAMING_SNAKE with at least
# one underscore, so ordinary capitalised words in prose ("TODO", "HTTP") are
# not mistaken for variables. Intersected with .env.example, so a match only
# counts when it is a real declared deployment variable.
_ENV_NAME_IN_TEXT = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")

_INTERPOLATION = re.compile(r"\$\{")


# --------------------------------------------------------------------------
# Deriving what the application reads
# --------------------------------------------------------------------------


class _EnvReadVisitor(ast.NodeVisitor):
    """Collects value-reads of environment variables.

    A *value* read is ``os.environ["X"]``, ``os.environ.get("X")`` or
    ``os.getenv("X")``. A membership probe — ``"X" in os.environ`` — is
    deliberately not one: `backend/src/data/db.py` uses that shape to detect
    that it is running under pytest, and forwarding ``PYTEST_VERSION`` into a
    production container would be a bug, not a fix. The distinction falls out
    of the node shapes rather than out of a name-based exception.
    """

    def __init__(self) -> None:
        self.literal_names: set[str] = set()
        self.dynamic_reads: set[str] = set()
        # Every `os.environ` node this visitor recognised as part of a known
        # form. Anything left over is a use of the mapping as a whole —
        # `{**os.environ}`, `dict(os.environ)`, `env = os.environ`,
        # `os.environ.update(...)` — which hides the names it touches from
        # both scans. Those become unresolvable sites rather than nothing.
        self._accounted: set[int] = set()
        self._environ_nodes: list[ast.expr] = []

    def _record(self, key: ast.expr, whole: ast.expr) -> None:
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            self.literal_names.add(key.value)
        else:
            self.dynamic_reads.add(ast.unparse(whole))

    @staticmethod
    def _is_environ(node: ast.expr) -> bool:
        # os.environ  (also environ, for `from os import environ`)
        if isinstance(node, ast.Attribute) and node.attr == "environ":
            return True
        return isinstance(node, ast.Name) and node.id == "environ"

    @staticmethod
    def _is_getenv(node: ast.expr) -> bool:
        if isinstance(node, ast.Attribute) and node.attr == "getenv":
            return True
        return isinstance(node, ast.Name) and node.id == "getenv"

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "environ":
            self._environ_nodes.append(node)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        # `from os import environ`. Not used in this repo today; collected so
        # that adopting it surfaces as an unresolvable site instead of a
        # silently unscanned module.
        if node.id == "environ":
            self._environ_nodes.append(node)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if self._is_environ(node.value):
            self._accounted.add(id(node.value))
            self._record(node.slice, node)
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        # `"X" in os.environ` — a probe, not a value read. Accounted for so it
        # does not fall into the leftover bucket, but never recorded.
        for comparator in node.comparators:
            if self._is_environ(comparator):
                self._accounted.add(id(comparator))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        environ_method = (
            isinstance(func, ast.Attribute)
            and func.attr in {"get", "setdefault", "pop"}
            and self._is_environ(func.value)
        )
        if environ_method:
            assert isinstance(func, ast.Attribute)
            self._accounted.add(id(func.value))
        if (environ_method or self._is_getenv(func)) and node.args:
            self._record(node.args[0], node)
        self.generic_visit(node)

    def leftover_environ_uses(self) -> set[str]:
        """`os.environ` touched as a whole mapping, hiding every name it reads."""
        return {
            ast.unparse(node)
            for node in self._environ_nodes
            if id(node) not in self._accounted
        }


def _python_sources() -> list[Path]:
    return sorted(APP_SOURCE_ROOT.rglob("*.py")) + list(ENTRYPOINT_SOURCES)


def _scan(paths: list[Path]) -> tuple[set[str], dict[str, set[str]]]:
    literals: set[str] = set()
    dynamic: dict[str, set[str]] = {}
    for path in paths:
        visitor = _EnvReadVisitor()
        visitor.visit(ast.parse(path.read_text()))
        literals |= visitor.literal_names
        unresolvable = visitor.dynamic_reads | visitor.leftover_environ_uses()
        if unresolvable:
            dynamic[path.relative_to(REPO_ROOT).as_posix()] = unresolvable
    return literals, dynamic


def _declared_in_env_example() -> set[str]:
    names = set()
    for line in ENV_EXAMPLE.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        names.add(stripped.split("=", 1)[0].strip())
    return names


def _mentioned_in_source(paths: list[Path], declared: set[str]) -> set[str]:
    """Declared deployment variables named anywhere in the application source.

    Catches the reads our own code does not perform — the SDK does — which is
    the ANTHROPIC_API_KEY case this whole module exists for.
    """
    found: set[str] = set()
    for path in paths:
        found |= set(_ENV_NAME_IN_TEXT.findall(path.read_text())) & declared
    return found


def _app_paths() -> list[Path]:
    return sorted(APP_SOURCE_ROOT.rglob("*.py"))


def app_env_vars() -> set[str]:
    """Every variable the containerised application reads, however it reads it.

    ``SDK_RESOLVED_VARIABLES`` is unioned in **after** the exemption ledger is
    subtracted, so an entry in rule 3 is structurally unreachable by rule 2's
    exemptions. That ordering is the fix for the hole where an SDK-resolved
    credential — found only by the text scan — could be exempted away.
    """
    app_paths = _app_paths()
    literals, _ = _scan(app_paths)
    mentioned = _mentioned_in_source(app_paths, _declared_in_env_example())
    return (
        literals
        | (mentioned - set(NOT_CONTAINER_VARIABLES))
        | set(SDK_RESOLVED_VARIABLES)
    )


def entrypoint_env_vars() -> set[str]:
    literals, _ = _scan(list(ENTRYPOINT_SOURCES))
    return literals | _mentioned_in_source(
        list(ENTRYPOINT_SOURCES), _declared_in_env_example()
    )


# --------------------------------------------------------------------------
# Deriving what each container gets
# --------------------------------------------------------------------------


def _load_compose(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text())
    assert isinstance(loaded, dict), path
    return loaded


def _service_env(service: dict[str, Any]) -> dict[str, str]:
    raw = service.get("environment") or {}
    if isinstance(raw, list):
        # `- NAME=value` / `- NAME` list form.
        out = {}
        for item in raw:
            name, _, value = str(item).partition("=")
            out[name] = value
        return out
    return {str(k): "" if v is None else str(v) for k, v in raw.items()}


def _app_image_services(compose: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Services that run the application image, whatever command they run.

    Keyed on the build stanza, which is how both stacks are written today.
    That is not sufficient on its own: once images are built in CI and the
    droplet pulls a prebuilt ``image:``, a service with no ``build:`` would
    become invisible to every assertion in this module and pass by finding
    nothing. ``test_no_application_service_escapes_detection`` closes that by
    checking the other direction — what the service actually runs — so the two
    have to disagree before anything slips through.
    """
    out = {}
    for name, service in (compose.get("services") or {}).items():
        build = service.get("build")
        dockerfile = build.get("dockerfile") if isinstance(build, dict) else None
        image = str(service.get("image", ""))
        if dockerfile == APP_DOCKERFILE or image.startswith(APP_IMAGE_PREFIX):
            out[name] = service
    return out


def _runs_application_code(service: dict[str, Any]) -> bool:
    """Does this service's command start our application, by any route?

    Deliberately keyed on the *command*, independent of how the image is
    obtained, so it stays true when builds move to CI.
    """
    command = service.get("command")
    if command is None:
        # No override: it runs the image's own CMD, which for the application
        # image is uvicorn. Only meaningful together with an app image, so an
        # unrelated service (redis, cloudflared) is not swept up here.
        build = service.get("build")
        dockerfile = build.get("dockerfile") if isinstance(build, dict) else None
        return dockerfile == APP_DOCKERFILE
    text = " ".join(command) if isinstance(command, list) else str(command)
    return any(marker in text for marker in APP_ENTRY_MARKERS)


COMPOSE_IDS = [p.relative_to(REPO_ROOT).as_posix() for p in COMPOSE_FILES]


# --------------------------------------------------------------------------
# The assertions
# --------------------------------------------------------------------------


def test_the_derivation_actually_found_something() -> None:
    """Guard against a silently-empty scan making every assertion vacuous.

    A refactor that moved every read behind a helper, or a glob that stopped
    matching, would otherwise turn this whole module green by finding nothing
    to check.
    """
    app = app_env_vars()
    assert len(app) >= 6, app
    # The three that would have caught the historical defects, by the three
    # different mechanisms this module uses to find them.
    assert "DATABASE_URL" in app  # os.getenv, literal key
    assert "PORTAL_TOKEN" in app  # os.environ.get, literal key
    assert "ANTHROPIC_API_KEY" in app  # read by the SDK; found only by text
    # And the one that must never be forwarded: a membership probe, not a read.
    assert "PYTEST_VERSION" not in app


def test_the_exemption_ledger_can_never_silence_a_real_requirement() -> None:
    """A variable the application genuinely needs is not exemptable, at any price.

    NOT_CONTAINER_VARIABLES exists to classify an ambiguous *mention*. It must
    not be able to cover either kind of real requirement:

    * an ``os.environ``/``os.getenv`` read (rule 1), or
    * an SDK-resolved credential (rule 3).

    The second half is a repair. The earlier version intersected the ledger
    with rule-1 literals only — and an SDK credential is *never* a literal
    read, so exactly the class rule 3 exists for was the class the ledger
    could silence. Measured: adding ``ANTHROPIC_API_KEY`` to the ledger
    dropped it out of the required set with every guard still green.

    Note the assertion is deliberately **not** ``ledger ∩ mentioned``: every
    ledger entry is required to be mentioned in the source (see the stale
    test), so that formulation would fail by construction and the ledger
    could not exist at all. Structure, not text, is what separates a
    cross-reference from a requirement.
    """
    literals, _ = _scan(_python_sources())
    protected = literals | set(SDK_RESOLVED_VARIABLES)
    overlap = sorted(protected & set(NOT_CONTAINER_VARIABLES))
    assert not overlap, (
        f"{overlap} are read by the application (directly, or by an SDK on "
        f"its behalf), so they cannot be listed as non-container variables. "
        f"If a container genuinely does not need one, the read — or the "
        f"dependency — is the thing to change."
    )


def test_every_sdk_resolved_credential_carries_its_evidence() -> None:
    """Rule 3 is a required list, so each line has to say why it is there.

    An entry with no stated resolver is indistinguishable from a name someone
    added to make a failure go away.
    """
    assert SDK_RESOLVED_VARIABLES, "rule 3 is empty; the SDK credentials are unchecked"
    for name, reason in SDK_RESOLVED_VARIABLES.items():
        assert name.isupper(), name
        assert len(reason) > 40, f"{name}'s justification is too thin: {reason!r}"


def test_every_sdk_resolved_credential_is_declared_in_env_example() -> None:
    """A credential nobody can find a line for is a credential nobody sets."""
    missing = sorted(set(SDK_RESOLVED_VARIABLES) - _declared_in_env_example())
    assert not missing, (
        f"{missing} are required in every container but .env.example has no "
        f"line to fill in"
    )


def test_the_exemption_ledger_has_no_stale_entries() -> None:
    """An entry whose mention is gone is a decision no longer being made."""
    mentioned = _mentioned_in_source(_app_paths(), _declared_in_env_example())
    stale = sorted(set(NOT_CONTAINER_VARIABLES) - mentioned)
    assert not stale, (
        f"{stale} are no longer named anywhere in backend/src/**; drop them "
        f"from NOT_CONTAINER_VARIABLES so the list stays a live record"
    )


def test_no_unresolvable_env_read_has_appeared_unnoticed() -> None:
    """Static analysis must declare what it cannot see.

    Every ``os.environ`` access with a non-literal key is recorded in
    KNOWN_DYNAMIC_ENV_READS with the reason it is safe. A new one fails here
    rather than being silently dropped from the required set.
    """
    _, dynamic = _scan(_python_sources())
    assert dynamic == KNOWN_DYNAMIC_ENV_READS, (
        "an environment read with a non-literal key changed. The forwarding "
        "audit cannot resolve it statically, so it is invisible to every "
        "other assertion in this module until someone decides whether the "
        "variable belongs in a container.\n"
        f"  found:    {dynamic}\n  recorded: {KNOWN_DYNAMIC_ENV_READS}"
    )


@pytest.mark.parametrize("compose_path", COMPOSE_FILES, ids=COMPOSE_IDS)
def test_every_compose_file_runs_the_application_image(compose_path: Path) -> None:
    """Otherwise the forwarding assertions below pass over an empty set."""
    services = _app_image_services(_load_compose(compose_path))
    assert services, (
        f"{compose_path.relative_to(REPO_ROOT)} defines no service building "
        f"{APP_DOCKERFILE}, so nothing in it is checked for env forwarding"
    )


@pytest.mark.parametrize("compose_path", COMPOSE_FILES, ids=COMPOSE_IDS)
def test_every_variable_the_app_reads_reaches_every_app_container(
    compose_path: Path,
) -> None:
    """W1-F3, the assertion the whole module is for.

    `backend` and `worker` run the same image (ADR-002) and are therefore
    checked against the same set. Slicing the requirement per service by
    import graph is exactly the reasoning that produced the "T-5's job / T-10's
    job" circle in `docs/STATE.md §2`: it is cheaper and safer to say that a
    container running the application gets the application's environment.
    """
    required = app_env_vars()
    compose = _load_compose(compose_path)
    for name, service in _app_image_services(compose).items():
        forwarded = set(_service_env(service))
        missing = sorted(required - forwarded)
        assert not missing, (
            f"{compose_path.relative_to(REPO_ROOT)} service '{name}' runs the "
            f"application image but does not forward {missing}. The "
            f"application reads {sorted(required)}; the service forwards "
            f"{sorted(forwarded)}."
        )


@pytest.mark.parametrize("compose_path", COMPOSE_FILES, ids=COMPOSE_IDS)
def test_entrypoint_variables_reach_every_service_that_runs_the_entrypoint(
    compose_path: Path,
) -> None:
    """`deploy/backend/bootstrap.py` runs from the image ENTRYPOINT.

    A service that overrides ``entrypoint:`` skips it and does not need its
    variables; a service that does not override it does. Derived from the
    compose key, so removing the worker's ``entrypoint: []`` override
    immediately requires SEED_ON_START on the worker.
    """
    required = entrypoint_env_vars()
    compose = _load_compose(compose_path)
    for name, service in _app_image_services(compose).items():
        if "entrypoint" in service:
            continue
        missing = sorted(required - set(_service_env(service)))
        assert not missing, (
            f"{compose_path.relative_to(REPO_ROOT)} service '{name}' runs the "
            f"image ENTRYPOINT (deploy/backend/entrypoint.sh -> bootstrap.py) "
            f"but does not forward {missing}"
        )


@pytest.mark.parametrize("compose_path", COMPOSE_FILES, ids=COMPOSE_IDS)
def test_no_required_variable_is_pinned_to_a_compose_local_constant(
    compose_path: Path,
) -> None:
    """Every forwarded value traces back to the host environment, or to this stack.

    `docs/deploy.md:151` requires ``set -a; source .env; set +a`` *before*
    ``docker compose ... up``, and this is the invariant that makes that
    instruction load-bearing rather than decorative: a required variable is
    either interpolated from the shell (so sourcing `.env` reaches it) or is a
    deliberate in-network override naming a service defined in this same file
    (``redis://redis:6379/0``, ``...@db:5432/...`` — where the host's own
    localhost-shaped value would be wrong inside a container).

    A bare literal that is neither is how a credential gets frozen into a
    committed file, or how a flag gets silently pinned to a value the operator
    cannot change.
    """
    required = app_env_vars() | entrypoint_env_vars()
    compose = _load_compose(compose_path)
    service_names = set(compose.get("services") or {})
    for name, service in _app_image_services(compose).items():
        for var, value in _service_env(service).items():
            if var not in required:
                continue
            if _INTERPOLATION.search(value):
                continue
            names_a_service = any(
                re.search(rf"(^|[@/:]){re.escape(svc)}(:|/|$)", value)
                for svc in service_names
            )
            assert names_a_service, (
                f"{compose_path.relative_to(REPO_ROOT)} service '{name}' pins "
                f"{var} to the literal {value!r}. A required variable must "
                f"either interpolate the host environment (${{{var}}}) or name "
                f"a service in this compose file."
            )


@pytest.mark.parametrize("compose_path", COMPOSE_FILES, ids=COMPOSE_IDS)
def test_no_application_service_escapes_detection(compose_path: Path) -> None:
    """Detection by build stanza and detection by command must agree.

    Every assertion above iterates ``_app_image_services``, which keys on
    ``build.dockerfile``. A service that pulled a prebuilt ``image:`` instead
    would silently drop out of all of them — the assertions would still pass,
    over a smaller set. This is the independent second opinion: anything whose
    *command* starts the application must also be classified as an application
    service.
    """
    compose = _load_compose(compose_path)
    detected = set(_app_image_services(compose))
    for name, service in (compose.get("services") or {}).items():
        if _runs_application_code(service):
            assert name in detected, (
                f"{compose_path.relative_to(REPO_ROOT)} service '{name}' runs "
                f"the application ({service.get('command')!r}) but is not "
                f"detected as an application service, so none of the env "
                f"forwarding assertions cover it. Teach _app_image_services "
                f"how this service gets its image."
            )


def _interpolated_names(node: Any) -> set[str]:
    """`${VAR}` names compose will actually interpolate.

    Walks the parsed YAML rather than the raw text, because the raw text also
    contains this repo's own comments explaining the ``${VAR}`` convention —
    prose that compose never sees and that would otherwise be reported as an
    undocumented variable named ``VAR``.
    """
    if isinstance(node, str):
        return set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)[:}]", node))
    if isinstance(node, dict):
        found: set[str] = set()
        for key, value in node.items():
            found |= _interpolated_names(key) | _interpolated_names(value)
        return found
    if isinstance(node, list):
        found = set()
        for item in node:
            found |= _interpolated_names(item)
        return found
    return set()


@pytest.mark.parametrize("compose_path", COMPOSE_FILES, ids=COMPOSE_IDS)
def test_env_example_covers_every_variable_compose_interpolates(
    compose_path: Path,
) -> None:
    """A `${VAR}` nobody documented is a knob nobody knows exists.

    ``BACKEND_PORT``, ``PORTAL_PORT`` and ``REDIS_PORT`` were each interpolated
    by a compose file and declared in neither `.env` nor `.env.example`, so the
    only way to discover them was to read the YAML.
    """
    declared = _declared_in_env_example()
    interpolated = _interpolated_names(_load_compose(compose_path))
    assert interpolated, f"{compose_path} interpolates nothing; assertion vacuous"
    missing = sorted(interpolated - declared)
    assert not missing, (
        f"{compose_path.relative_to(REPO_ROOT)} interpolates {missing}, which "
        f".env.example never mentions"
    )


def test_env_example_declares_every_variable_the_scripts_read() -> None:
    """`scripts/**` is not containerised, but it is still run by a human.

    Excluded from forwarding on purpose — nothing runs it in a container — so
    this is the one thing that still has to be true of it: every variable it
    resolves has a line someone can fill in.
    """
    script_paths = sorted((REPO_ROOT / "scripts").rglob("*.py"))
    assert script_paths, "no scripts found; this assertion would be vacuous"
    literals, _ = _scan(script_paths)
    missing = sorted(literals - _declared_in_env_example())
    assert not missing, (
        f"scripts/** read {missing} but .env.example never mentions them"
    )


def test_env_example_declares_every_variable_the_app_reads_explicitly() -> None:
    """`.env.example` is what a fresh clone copies to `.env`.

    Only the statically-resolved reads are asserted: those are unambiguously
    ours. (The text-derived ones are, by construction, already in
    `.env.example` — that is where the name came from.)
    """
    literals, _ = _scan(_python_sources())
    declared = _declared_in_env_example()
    # PYTEST_VERSION-style probes never reach here (they are not value reads);
    # anything that does is a variable a deployed process actually resolves.
    missing = sorted(literals - declared)
    assert not missing, (
        f"the application reads {missing} but .env.example never mentions "
        f"them, so a fresh clone's .env has no line to fill in"
    )
