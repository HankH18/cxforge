"""Shared helpers for backend/tests/plan/.

Not a test module itself (pytest only collects ``test_*.py``); imported by
test_blast_radius.py and test_self_gating.py.

Two things live here:

1. A first-party Python import graph, built by walking backend/src/**,
   backend/tests/** and evals/** with ``ast`` and recording which
   known first-party top-level modules each file imports. This is a REAL,
   computed graph (not a hand-typed table asserting today's known gaps) so
   that a future ticket which adds or removes an import changes what the
   plan tests require without anyone touching this file. It is what lets
   ``ImportGraph.reverse_deps(pkg)`` answer "every test suite that imports
   from `pkg`, directly or transitively" -- the reverse-dependency set
   acceptance 1 (T-14) requires.

2. A parser for a ticket's ``verify`` string (``analyze_verify``) and the
   decidable self-gating tokenisation rule from T-14's contract, used
   verbatim including the test-runner-with-directory-argument exemption
   (``find_self_gating_violations``).

Both are driven entirely off docs/tickets.json and the actual source tree --
nothing here hard-codes "ticket T-X should fail".
"""

from __future__ import annotations

import ast
import json
import posixpath
import re
import shlex
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
TICKETS_PATH = REPO_ROOT / "docs" / "tickets.json"


# --------------------------------------------------------------------------
# docs/tickets.json
# --------------------------------------------------------------------------


def load_tickets() -> list[dict]:
    data = json.loads(TICKETS_PATH.read_text())
    return data["tickets"]


# --------------------------------------------------------------------------
# Glob matching -- mirrors .claude/hooks/scope_guard.sh's glob_to_regex
# semantics exactly (anchored both ends, "**" crosses "/", a lone "*" does
# not, every other character including "." is literal). Scope entries in
# docs/tickets.json are matched against the SAME rule the live scope guard
# uses, so a plan test that reuses this logic can't drift from what the
# hook actually enforces.
# --------------------------------------------------------------------------


def glob_to_regex(glob: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    n = len(glob)
    while i < n:
        if glob[i : i + 2] == "**":
            out.append(".*")
            i += 2
            continue
        c = glob[i]
        if c == "*":
            out.append("[^/]*")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^(?:" + "".join(out) + ")$")


def path_matches_any_glob(path: str, globs: list[str]) -> bool:
    return any(glob_to_regex(g).match(path) for g in globs)


# --------------------------------------------------------------------------
# First-party import graph
# --------------------------------------------------------------------------

# Top-level import roots that resolve to this repo's own code, per
# pyproject.toml's [tool.pytest.ini_options].pythonpath = ["backend/src",
# "."] -- anything else (fastapi, pytest, yaml, ...) is third-party and out
# of scope for a blast-radius graph.
FIRST_PARTY_ROOTS = {
    "data",
    "helpdesk",
    "agent",
    "escalation",
    "ingress",
    "portal",
    "main",
    "evals",
}


def _iter_py_files(*roots: Path) -> Iterator[Path]:
    for root in roots:
        if not root.exists():
            continue
        yield from root.rglob("*.py")


def _first_party_imports(py_file: Path) -> set[str]:
    """Top-level first-party module names a file imports."""
    try:
        tree = ast.parse(py_file.read_text(), filename=str(py_file))
    except SyntaxError:
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in FIRST_PARTY_ROOTS:
                    found.add(root)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue  # relative import within the same package: not a cross-node edge
            if node.module:
                root = node.module.split(".")[0]
                if root in FIRST_PARTY_ROOTS:
                    found.add(root)
    return found


def _node_identity(py_file: Path) -> str | None:
    """Map a source file to the graph node it belongs to.

    backend/src/<pkg>/**   -> "<pkg>"        (a first-party package)
    backend/src/main.py    -> "main"
    backend/tests/<dir>/** -> "test:<dir>"   (a test suite directory)
    evals/**               -> "evals"
    anything else          -> None (not part of the graph)
    """
    rel = py_file.relative_to(REPO_ROOT)
    parts = rel.parts
    if parts[0:2] == ("backend", "src"):
        if len(parts) == 3 and parts[2] == "main.py":
            return "main"
        if len(parts) >= 3:
            return parts[2]
        return None
    if parts[0:2] == ("backend", "tests"):
        if len(parts) >= 3:
            return f"test:{parts[2]}"
        return None
    if parts[0] == "evals":
        return "evals"
    return None


@dataclass(frozen=True)
class ImportGraph:
    # direct first-party module imports per node ("<pkg>" or "test:<dir>")
    direct: dict[str, set[str]] = field(default_factory=dict)

    def transitive(self, node: str, _seen: frozenset[str] | None = None) -> set[str]:
        """Every first-party module `node` imports, directly or via chains
        through other first-party modules. Cycle-safe (agent <-> escalation
        import each other in this codebase)."""
        seen = set(_seen or set())
        if node in seen:
            return set()
        seen.add(node)
        result: set[str] = set()
        for dep in self.direct.get(node, set()):
            result.add(dep)
            result |= self.transitive(dep, frozenset(seen))
        return result

    def reverse_deps(self, package: str) -> set[str]:
        """Every "test:<dir>" node that imports `package`, directly or
        transitively (e.g. via backend/src/main.py importing both the
        ingress and portal routers, which a test suite's conftest pulls in
        with `from main import app`)."""
        hits: set[str] = set()
        for node in self.direct:
            if not node.startswith("test:"):
                continue
            if package in self.transitive(node):
                hits.add(node)
        return hits


def build_import_graph() -> ImportGraph:
    direct: dict[str, set[str]] = {}
    for py_file in _iter_py_files(
        REPO_ROOT / "backend" / "src",
        REPO_ROOT / "backend" / "tests",
        REPO_ROOT / "evals",
    ):
        node = _node_identity(py_file)
        if node is None:
            continue
        imports = _first_party_imports(py_file)
        imports.discard(node)  # a package importing its own submodules isn't a cross-node edge
        direct.setdefault(node, set()).update(imports)
    return ImportGraph(direct=direct)


# --------------------------------------------------------------------------
# Ticket scope -> touched packages / owned test dirs / npm(frontend)
#
# These use REAL glob matching (glob_to_regex, the same semantics
# scope_guard.sh enforces) against representative probe paths, rather than
# literal string prefixes -- so a scope entry like T-0's "backend/**" (which
# covers every package and every test dir) is handled correctly instead of
# silently under-detected.
# --------------------------------------------------------------------------

KNOWN_PACKAGES = ["data", "helpdesk", "agent", "escalation", "ingress", "portal"]

# Every backend/tests/<dir> that exists today, PLUS the dirs later tickets'
# scope already names before their suite exists on disk (T-10's "live",
# T-17's "deploy", and this ticket's own "plan") -- probing is pure glob
# matching against a synthetic path, so the directory need not exist yet.
KNOWN_TEST_DIRS = [
    "contract",
    "data",
    "escalation",
    "evals",
    "graph",
    "grounding",
    "hooks",
    "ingress",
    "portal",
    "plan",
    "live",
    "deploy",
]

HOOKS_TRIGGER_PATH = "docs/tickets.json"


def _glob_reaches_prefix(glob: str, prefix: tuple[str, ...]) -> bool:
    """True iff `glob` (T-14's scope-glob syntax: "**" crosses "/", a lone
    "*" matches exactly one path segment, anything else is literal -- see
    glob_to_regex) can match at least one repo-relative path that lives at
    or under the literal directory `prefix` (e.g. ("backend", "src",
    "helpdesk")) -- WITHOUT requiring any concrete file at that path to
    exist on disk, and without needing to guess which single filename
    inside that directory the scope entry happens to name.

    This replaces probing a single hard-coded literal path (e.g.
    "backend/src/<pkg>/__init__.py" or "backend/tests/<dir>/conftest.py"):
    a scope entry that names one individual file inside a package -- T-3's
    "backend/src/helpdesk/email_adapter.py", not a
    "backend/src/helpdesk/**" wildcard -- has no reason to be named
    "__init__.py", so a literal-probe check silently reports "package not
    touched" for it (the exact defect an adversarial review caught: T-3's
    verify ran only the contract suite while its scope's real
    reverse-dependency set -- graph, grounding, ingress, portal, evals,
    escalation -- went uncovered). Comparing glob segments against the
    prefix's segments instead catches every shape: a directory wildcard, a
    single wildcard segment, a bare file at the prefix, or one specific
    file several levels inside it -- and needs no source tree to exist."""
    g_segs = glob.split("/")
    i = 0  # index into prefix
    for seg in g_segs:
        if seg == "**":
            return True  # "**" can consume the rest of the prefix and beyond
        if i >= len(prefix):
            return True  # glob names something *inside* the prefix directory
        if seg == "*" or seg == prefix[i]:
            i += 1
            continue
        return False  # literal segment mismatch: this glob lives elsewhere
    return i >= len(prefix)  # exhausted the glob only after covering the prefix


def scope_touched_packages(scope: list[str]) -> set[str]:
    """Which backend/src packages (plus evals) a ticket's scope can change."""
    touched: set[str] = set()
    for pkg in KNOWN_PACKAGES:
        if any(_glob_reaches_prefix(g, ("backend", "src", pkg)) for g in scope):
            touched.add(pkg)
    if any(_glob_reaches_prefix(g, ("evals",)) for g in scope):
        touched.add("evals")
    return touched


def scope_owned_test_dirs(scope: list[str]) -> set[str]:
    """Which backend/tests/<dir> suites are literally part of this ticket's
    own declared scope -- these are "its own suite" under acceptance 1,
    regardless of what the import graph says."""
    owned: set[str] = set()
    for d in KNOWN_TEST_DIRS:
        if any(_glob_reaches_prefix(g, ("backend", "tests", d)) for g in scope):
            owned.add(d)
    return owned


def scope_needs_npm_portal(scope: list[str]) -> bool:
    """Whether the ticket's scope touches the portal/** FRONTEND tree
    (distinct from the backend/src/portal/** Python package -- nothing
    Python imports the frontend, so this is checked independently of the
    import graph, purely via glob/prefix overlap)."""
    return any(_glob_reaches_prefix(g, ("portal",)) for g in scope)


def scope_touches_tickets_json(scope: list[str]) -> bool:
    """Non-import reverse dependency (T-14 job spec): .claude/hooks/
    scope_guard.sh and verify_gate.sh both `jq` docs/tickets.json directly,
    and backend/tests/hooks/conftest.py copies the REAL docs/tickets.json
    into every hook test's fixture project -- so any ticket that can edit
    docs/tickets.json has backend/tests/hooks in its reverse-dependency set
    even though nothing "imports" JSON in the Python sense."""
    return path_matches_any_glob(HOOKS_TRIGGER_PATH, scope)


def required_test_dirs_for_ticket(ticket: dict, graph: ImportGraph) -> set[str]:
    """The full acceptance-1 blast-radius set for one ticket: its own
    declared suite(s), plus every suite that imports (directly or
    transitively) from a package its scope can change, plus the hooks
    non-import special case."""
    scope = ticket["scope"]
    required = set(scope_owned_test_dirs(scope))
    for pkg in scope_touched_packages(scope):
        required |= {node.split(":", 1)[1] for node in graph.reverse_deps(pkg)}
    if scope_touches_tickets_json(scope):
        required.add("hooks")
    return required


# --------------------------------------------------------------------------
# verify-string coverage analysis (acceptance 1's "does the verify command
# actually run what's required")
# --------------------------------------------------------------------------

# Registered in pyproject.toml [tool.pytest.ini_options].markers -- each
# marker's own docstring there names the single ticket/suite it belongs to,
# so this 1:1 mapping is authoritative, not a guess.
MARKER_TO_DIRS = {
    "contract": {"contract"},
    "grounding": {"grounding"},
    "live": {"live"},
}


@dataclass(frozen=True)
class VerifyCoverage:
    covered_test_dirs: set[str]
    is_full_suite: bool
    covers_npm_portal: bool


def _split_clauses(verify: str) -> list[list[str]]:
    """Tokenise the WHOLE verify string once with shlex (so a quoted
    argument -- e.g. T-7's `python -c "...; ...; ..."` -- keeps its
    embedded ";" / "&&"-looking substrings intact as part of one token),
    then split the flat token stream on bare "&&"/";" operator tokens."""
    tokens = shlex.split(verify)
    clauses: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in ("&&", ";"):
            if current:
                clauses.append(current)
            current = []
        else:
            current.append(tok)
    if current:
        clauses.append(current)
    return clauses


def analyze_verify(verify: str) -> VerifyCoverage:
    covered: set[str] = set()
    full_suite = False
    npm_portal = False
    saw_cd_portal = False

    for tokens in _split_clauses(verify):
        if not tokens:
            continue
        t = tokens[2:] if tokens[:2] == ["uv", "run"] else tokens

        if t[:2] == ["cd", "portal"]:
            saw_cd_portal = True
            continue

        if t[0] == "npm":
            if saw_cd_portal:
                npm_portal = True
            continue

        if t[0] == "pytest":
            args = t[1:]
            marker: str | None = None
            paths: list[str] = []
            i = 0
            while i < len(args):
                a = args[i]
                if a == "-m":
                    marker = args[i + 1] if i + 1 < len(args) else None
                    i += 2
                    continue
                if a.startswith("-"):
                    i += 1
                    continue
                paths.append(a)
                i += 1

            if paths:
                for p in paths:
                    parts = Path(p).parts
                    if parts[:2] == ("backend", "tests") and len(parts) >= 3:
                        covered.add(parts[2])
                    elif parts[:2] == ("backend", "tests") and len(parts) == 2:
                        full_suite = True  # bare "backend/tests" positional
                if marker in MARKER_TO_DIRS:
                    covered |= MARKER_TO_DIRS[marker]
            else:
                # no positional path: bare pytest collects testpaths =
                # ["backend/tests"], i.e. everything -- unless narrowed to a
                # single marker's subset.
                if marker is None or marker == "not live":
                    full_suite = True
                elif marker in MARKER_TO_DIRS:
                    covered |= MARKER_TO_DIRS[marker]
            continue

        # anything else (bash/sh scripts, `python -c ...`, `python -m ...`)
        # contributes no *test-suite* coverage by itself.

    return VerifyCoverage(
        covered_test_dirs=covered,
        is_full_suite=full_suite,
        covers_npm_portal=npm_portal,
    )


# --------------------------------------------------------------------------
# acceptance 2: self-gating tokenisation rule, verbatim from T-14's
# contract --
#
#   "tokenise the verify command, take every token that is a repo-relative
#   path or is an argument to bash/sh, and fail if any such path matches
#   that ticket's own scope globs. Test-runner invocations (pytest / npm /
#   uv run pytest) with a DIRECTORY argument are explicitly EXEMPT."
#
# Deliberately literal: a token only counts as "a repo-relative path" if it
# contains "/" (T-7's `uv run python -m evals.report` uses a DOTTED module
# path, not a slash path, so this literal rule does not flag it -- see the
# self-gating scan report for that near-miss; T-14's contract says to use
# THIS rule verbatim, not a semantically-widened one).
# --------------------------------------------------------------------------


def _is_repo_relative_path_token(tok: str) -> bool:
    if tok.startswith("-"):
        return False
    if tok.startswith(("http://", "https://")):
        return False
    return "/" in tok


def _normalize_repo_relative(tok: str) -> str:
    """Collapse an ordinary shell path spelling (a leading "./", doubled
    "//", a "../" segment) to the same repo-relative form scope globs are
    written in. Without this, `bash ./scripts/verify_deploy.sh` -- exactly
    as self-gating as `bash scripts/verify_deploy.sh` -- silently evades
    `path_matches_any_glob`, because glob_to_regex requires an EXACT
    (anchored) string match and "./scripts/verify_deploy.sh" != T-11's
    scope entry "scripts/verify_deploy.sh". Applied only to tokens already
    identified as a repo-relative-path candidate or a bash/sh argument, so
    it never touches flags or non-path tokens."""
    norm = posixpath.normpath(tok)
    return norm[2:] if norm.startswith("./") else norm


def _pytest_or_npm_directory_positionals(tokens: list[str]) -> set[str]:
    """Positional (non-flag) arguments to a pytest/npm invocation, treated
    as the exempt "directory argument" shape (acceptance 2's carve-out)."""
    base = tokens[2:] if tokens[:2] == ["uv", "run"] else tokens
    if not base or base[0] not in ("pytest", "npm"):
        return set()
    positionals: set[str] = set()
    skip_next = False
    for tok in base[1:]:
        if skip_next:
            skip_next = False
            continue
        if tok == "-m":
            skip_next = True
            continue
        if tok.startswith("-"):
            continue
        if not tok.endswith(".py") and not tok.endswith(".sh"):
            positionals.add(tok)
    return positionals


def find_self_gating_violations(ticket: dict) -> list[str]:
    """Tokens in `ticket`'s verify command that are (a) a repo-relative
    path, or (b) an argument to bash/sh, AND (c) match `ticket`'s own scope
    globs, AND (d) are not exempted as a test-runner directory argument."""
    scope = ticket["scope"]
    violations: list[str] = []

    for tokens in _split_clauses(ticket["verify"]):
        if not tokens:
            continue
        exempt = {
            _normalize_repo_relative(e) for e in _pytest_or_npm_directory_positionals(tokens)
        }

        candidates = [t for t in tokens if _is_repo_relative_path_token(t)]
        if tokens[0] in ("bash", "sh"):
            candidates = list(dict.fromkeys(candidates + tokens[1:]))

        for tok in candidates:
            normalized = _normalize_repo_relative(tok)
            if normalized in exempt:
                continue
            if path_matches_any_glob(normalized, scope):
                violations.append(tok)

    return sorted(set(violations))
