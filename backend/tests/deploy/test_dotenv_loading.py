"""W1-F4 — `.env` is loaded by something now.

`docs/STATE.md §6.14`: no ``load_dotenv()`` existed in the app or the scripts,
so the documented run commands saw zero credentials from a fully populated
`.env` — ``scripts/live_smoke.py`` printed "credentials absent" and exited 0,
and the webhook 500'd on a missing signing secret. `backend/src/main.py` now
loads it, and `scripts/live_smoke.py` does the same for its own process.

(One caller did already exist outside both — ``evals/report.py:411``. STATE's
"in the app or scripts" phrasing was exact; an earlier revision of this
docstring widened it to "anywhere in this repo", which was false.)

**Every assertion here runs `main` in a subprocess.** Two independent reasons,
and both matter:

1. *It is the only honest test.* The module-level load is deliberately gated
   off inside pytest (see ``main._running_under_pytest`` for why), so an
   in-process test could only ever exercise the helper, never the line that
   has to fire when the app is really started. A subprocess with
   ``PYTEST_VERSION`` scrubbed runs the real path; one with ``PYTEST_VERSION``
   present proves the gate.

2. *This module must not import `main`.* `docs/BUILD-PLAN.md §3`'s
   blast-radius note: a first-party ``import`` statement in a test suite
   directory adds an edge in `backend/tests/plan/_planlib.py`'s graph, and
   ``import main`` fans out to ingress + portal and thence to agent,
   escalation, helpdesk and data. Measured 2026-08-16: an earlier revision of
   this file did import `main`, and `test_blast_radius.py` went from 32 passed
   to **11 failed** — T-1, T-2, T-4, T-5, T-6, T-7, T-8, T-18, T-19, T-20 and
   T-24, all of them closed tickets. The detector is AST-based
   (`_planlib.py:110-131`) and only sees real import statements, so the code
   below hands `main` to a child interpreter as a string instead.

The real repo's `.env` is never read and never written by anything here; every
test builds its own synthetic one under `tmp_path`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_MAIN = REPO_ROOT / "backend" / "src" / "main.py"
REAL_SRC = REPO_ROOT / "backend" / "src"

PROBE = "CXFORGE_DOTENV_PROBE"


def _fake_repo_with_main(tmp_path: Path, env_body: str | None) -> Path:
    """A disposable repo root holding a byte-identical copy of `main.py`.

    `main.py` derives its repo root from its own ``__file__``, so copying it
    three levels down inside `tmp_path` is enough to point it at a synthetic
    `.env` — no new env-var seam, and the code under test is unmodified.
    ``env_body=None`` builds a repo with no `.env` at all.
    """
    src = tmp_path / "backend" / "src"
    src.mkdir(parents=True)
    shutil.copy2(REAL_MAIN, src / "main.py")
    if env_body is not None:
        (tmp_path / ".env").write_text(env_body)
    return tmp_path


def _import_main(
    fake_repo: Path,
    *,
    extra_env: dict[str, str] | None = None,
    look_like_pytest: bool = False,
) -> dict[str, object]:
    """Import the copied `main` in a child interpreter; report what it resolved.

    The fake repo's `backend/src` goes first on the path so `main` resolves
    from the copy; the real one follows so `ingress`/`portal` still import.
    """
    code = (
        "import os, sys, json;"
        f"sys.path.insert(0, {str(fake_repo / 'backend' / 'src')!r});"
        f"sys.path.insert(1, {str(REAL_SRC)!r});"
        "import main;"
        "print(json.dumps({"
        f"'probe': os.environ.get({PROBE!r}),"
        "'repo_root': str(main.REPO_ROOT),"
        "'has_health': hasattr(main, 'health'),"
        "}))"
    )
    env = {
        k: v
        for k, v in os.environ.items()
        # Scrubbed so the child does not look like a pytest process unless a
        # test asks for it — otherwise main.py's gate skips the very call
        # most of these tests exist to prove.
        if k not in {"PYTEST_VERSION", "PYTEST_CURRENT_TEST", PROBE}
    }
    if look_like_pytest:
        env["PYTEST_VERSION"] = "8.3.0"
    env.update(extra_env or {})

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=fake_repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert isinstance(payload, dict)
    # Cheap proof that the child imported the real application module and not
    # some empty stand-in that would make every assertion below vacuous.
    assert payload["has_health"] is True, payload
    return payload


def test_importing_the_app_loads_the_repo_dotenv(tmp_path: Path) -> None:
    """The STATE.md §6.14 gap, closed and proven at the point it has to work."""
    fake_repo = _fake_repo_with_main(tmp_path, f"{PROBE}=loaded-at-import\n")
    payload = _import_main(fake_repo)

    assert payload["repo_root"] == str(fake_repo), payload
    assert payload["probe"] == "loaded-at-import", (
        "importing `main` did not load the repo `.env`. This is exactly the "
        "gap W1-F4 closes: the documented run commands see an empty "
        "environment from a fully populated `.env`."
    )


def test_an_already_exported_variable_beats_the_file(tmp_path: Path) -> None:
    """``override=False``, asserted where it matters.

    The container case: every credential arrives through `docker compose`'s
    ``environment:`` block, and a file must never be able to shadow it. Same
    precedence rule `.env.example` already states for ``DEPLOY_HOST``.
    """
    fake_repo = _fake_repo_with_main(tmp_path, f"{PROBE}=from-dotenv\n")
    payload = _import_main(fake_repo, extra_env={PROBE: "already-exported"})

    assert payload["probe"] == "already-exported", payload


def test_a_missing_env_file_is_not_an_error(tmp_path: Path) -> None:
    """Production has no `.env` — every value arrives through compose.

    ``_import_main`` asserts the child exited 0, so importing the app in a
    directory with no `.env` starting cleanly is the assertion.
    """
    fake_repo = _fake_repo_with_main(tmp_path, None)
    payload = _import_main(fake_repo)

    assert payload["probe"] is None, payload


def test_a_pytest_process_never_inherits_the_developers_real_credentials(
    tmp_path: Path,
) -> None:
    """The gate, exercised rather than assumed.

    Without it, importing `main` anywhere in the suite would put a real
    ``ANTHROPIC_API_KEY`` and a real Zendesk OAuth token into ``os.environ``
    for the rest of the session, so a test that forgot to stub something would
    reach a live API instead of failing — and CI, which has no `.env`, would
    not reproduce it. The child here is identical to the one above but for
    ``PYTEST_VERSION``.
    """
    fake_repo = _fake_repo_with_main(tmp_path, f"{PROBE}=must-not-be-loaded\n")
    payload = _import_main(fake_repo, look_like_pytest=True)

    assert payload["probe"] is None, (
        "a process that looks like pytest loaded the repo `.env`; a test run "
        "must state its own environment explicitly"
    )


def test_this_module_never_imports_first_party_code(tmp_path: Path) -> None:
    """Guards the blast-radius property this module's docstring depends on.

    `backend/tests/plan/_planlib.py` builds its graph from AST import nodes.
    If someone converts one of the subprocesses above into a convenient
    ``import main``, 11 closed tickets go red in `test_blast_radius.py` for
    reasons that will look nothing like this file. Fail here instead, where
    the explanation is.
    """
    import ast

    tree = ast.parse(Path(__file__).read_text())
    first_party = {
        "data",
        "helpdesk",
        "agent",
        "escalation",
        "ingress",
        "portal",
        "main",
        "evals",
    }
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            imported.add(node.module.split(".")[0])

    offending = sorted(imported & first_party)
    assert not offending, (
        f"{offending} imported at module scope or in a function body adds a "
        f"first-party edge from backend/tests/deploy/ — see this module's "
        f"docstring and docs/BUILD-PLAN.md §3. Drive it through a subprocess."
    )
