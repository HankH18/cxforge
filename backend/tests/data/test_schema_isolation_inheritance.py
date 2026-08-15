"""T-23 acceptance 3 regression test.

``backend/tests/evals/test_no_docs_writes.py`` spawns a CHILD pytest
process as a subprocess (``subprocess.run([sys.executable, "-m", "pytest",
...])``), with no ``env=`` override. ``subprocess.run``'s default behavior
(no ``env=`` kwarg) is to inherit the CURRENT process's environment
wholesale -- so the child does NOT fail to see ``OTHRAM_TEST_SCHEMA``
(empirically confirmed: it resolves the exact same value, and the exact
same real Postgres ``current_schema()``, as its parent). The bug is the
opposite of what that might suggest, and worse: the child inherits the
SAME schema NAME as its parent -- not an isolated schema of its own, the
literal same physical schema the parent (and the rest of the still-running
full suite) is actively using.

``backend/tests/conftest.py``'s ``pytest_sessionfinish`` hook -- imported
and run by every pytest process, including this spawned child, because
``backend/tests/conftest.py`` is the pytest root conftest for
``testpaths = ["backend/tests"]`` -- unconditionally executes ``DROP
SCHEMA IF EXISTS <own> CASCADE`` at the end of every session, where "own"
is simply whatever ``OTHRAM_TEST_SCHEMA`` currently resolves to for that
process. It never distinguishes "a schema this process itself derived and
owns" from "a schema this process merely inherited from an ancestor's
environment". So when the child's own session finishes, it CASCADE-drops
the schema its PARENT is still actively using -- destroying every table
and row the parent (and the rest of the enclosing full-suite run) depends
on, mid-suite, silently: ``data.db.get_connection`` unconditionally
re-issues ``CREATE SCHEMA IF NOT EXISTS`` on every subsequent connection,
so the schema NAME reappears (empty) the instant anything reconnects,
while its DATA is gone. A naive "does the child resolve the same schema
NAME as its parent" check cannot see this at all -- the names genuinely
do match; that is precisely the problem, not the fix.

The correct behavior -- "the child inherits the parent's schema
ISOLATION" (T-23 acceptance 3's own wording) -- is for the child pytest
process to get its OWN private, independently-derived schema (exactly
what ``backend/tests/conftest.py``'s ``_derive_test_schema()`` already
does, correctly, for any process that starts with ``OTHRAM_TEST_SCHEMA``
unset -- the very same per-process derivation T-16 established), so its
own teardown only ever drops a schema it exclusively owns and the
parent's live schema is never touched. Since ``backend/tests/conftest.py``
is out of scope for this ticket's fix (owned by a different, concurrent
acceptance item), the only fix available here is at the spawn site itself
in ``backend/tests/evals/test_no_docs_writes.py``: give the child an
explicit ``env=`` that no longer carries the parent's
``OTHRAM_TEST_SCHEMA`` value, so the child's own conftest derives a fresh,
independent one for itself.

Two regression tests below, both driving the REAL
``backend/tests/evals/test_no_docs_writes.py::test_evals_suite_leaves_docs_untouched``
function (loaded directly from its file via ``importlib``, bypassing
pytest's own collection machinery, so both tests exercise the actual
current subprocess.run call in that file -- not a hand-rolled mirror of
it -- and both flip from failing to passing purely by that file being
fixed, with no change needed here):

* ``test_no_docs_writes_child_does_not_drop_the_parents_live_schema`` is
  the end-to-end proof: creates a real marker row in this (parent)
  process's own live schema, calls the real function (which spawns a REAL
  child subprocess exactly as it does today), and asserts the marker
  survives -- the direct, load-bearing regression proof for "the current
  mid-suite drop", reproducing it against unfixed code and proving it
  gone against fixed code.
* ``test_no_docs_writes_child_env_excludes_parent_schema_override`` is the
  fast, root-cause-precise complement: monkeypatches ``subprocess.run``
  inside the loaded module to capture the ``env=`` it was actually called
  with (without spawning a real child at all) and asserts that env no
  longer carries the parent's ``OTHRAM_TEST_SCHEMA`` value -- proving the
  CAUSE is fixed, not just observing a downstream effect.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import types
import uuid
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

import psycopg
import pytest

from data.db import TEST_SCHEMA_ENV_VAR, get_connection

pytestmark = pytest.mark.skipif(
    os.getenv("SKIP_DB_TESTS") == "1",
    reason="requires the docker-compose db service (CI sets SKIP_DB_TESTS=1)",
)

# backend/tests/data/test_schema_isolation_inheritance.py -> data -> tests -> backend -> repo root
REPO_ROOT = Path(__file__).resolve().parents[3]
_NO_DOCS_WRITES_PATH = REPO_ROOT / "backend" / "tests" / "evals" / "test_no_docs_writes.py"

_MARKER_TABLE = "t23_schema_survives_child_marker"


def _load_no_docs_writes_module() -> ModuleType:
    """Load backend/tests/evals/test_no_docs_writes.py directly from its
    file path, as an ordinary Python import outside pytest's own
    collection -- so calling its functions here runs the SAME code that
    file actually contains right now, not a copy of it."""
    spec = importlib.util.spec_from_file_location(
        "_t23_probe_test_no_docs_writes", _NO_DOCS_WRITES_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_docs_writes_child_does_not_drop_the_parents_live_schema() -> None:
    """Regression test for T-23 acceptance 3 (end-to-end proof).

    Reproduces "the current mid-suite drop": a real marker row created in
    this process's own live schema must still be there after
    backend/tests/evals/test_no_docs_writes.py's real
    test_evals_suite_leaves_docs_untouched() runs -- which, exactly as it
    does in a real full-suite run, spawns a genuine child pytest
    subprocess. Before the fix, that child inherits this process's own
    live schema and CASCADE-drops it at its own teardown; after the fix,
    the child gets an independent schema of its own and never touches
    this one.
    """
    try:
        parent_schema = os.environ[TEST_SCHEMA_ENV_VAR]
    except KeyError:
        pytest.fail(
            f"{TEST_SCHEMA_ENV_VAR} must be set by backend/tests/conftest.py under pytest"
        )

    marker_value = uuid.uuid4().hex
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {_MARKER_TABLE}")
            cur.execute(f"CREATE TABLE {_MARKER_TABLE} (marker text PRIMARY KEY)")
            cur.execute(f"INSERT INTO {_MARKER_TABLE} (marker) VALUES (%s)", (marker_value,))
    except psycopg.OperationalError as exc:
        pytest.skip(f"database unreachable: {exc}")

    module = _load_no_docs_writes_module()
    try:
        module.test_evals_suite_leaves_docs_untouched()
    finally:
        # Regardless of that call's own outcome, check whether this
        # process's schema and its marker row survived it.
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT schema_name FROM information_schema.schemata WHERE schema_name = %s",
                (parent_schema,),
            )
            schema_still_exists = cur.fetchone() is not None
            marker_survived = False
            if schema_still_exists:
                cur.execute(
                    f"SELECT 1 FROM {_MARKER_TABLE} WHERE marker = %s", (marker_value,)
                )
                marker_survived = cur.fetchone() is not None
            cur.execute(f"DROP TABLE IF EXISTS {_MARKER_TABLE}")

    assert schema_still_exists, (
        f"this process's own live schema {parent_schema!r} no longer exists after "
        "backend/tests/evals/test_no_docs_writes.py's child pytest subprocess ran -- "
        "the child CASCADE-dropped the schema its parent was still actively using."
    )
    assert marker_survived, (
        f"a marker row inserted into this process's own live schema {parent_schema!r} "
        "before spawning the child did not survive the child pytest subprocess spawned "
        "by backend/tests/evals/test_no_docs_writes.py -- this is T-23 acceptance 3's "
        "'current mid-suite drop': the child inherited and then CASCADE-dropped the "
        "parent's live schema at its own teardown, silently destroying its data (the "
        "schema name itself reappears, empty, the moment anything next reconnects, "
        "which is why a schema-NAME-only check cannot see this)."
    )


def test_no_docs_writes_child_env_excludes_parent_schema_override() -> None:
    """Regression test for T-23 acceptance 3 (root-cause proof).

    Captures the actual `env=` kwarg backend/tests/evals/
    test_no_docs_writes.py's real subprocess.run call is invoked with
    (subprocess.run itself is stubbed out here -- no real child process is
    spawned, so this runs in milliseconds) and asserts it no longer hands
    the child this process's own OTHRAM_TEST_SCHEMA value, which is what
    causes the child to inherit -- and then, at its own teardown, CASCADE
    -drop -- the parent's live schema (see
    test_no_docs_writes_child_does_not_drop_the_parents_live_schema and
    this module's docstring for the end-to-end proof and mechanism).
    """
    try:
        parent_schema = os.environ[TEST_SCHEMA_ENV_VAR]
    except KeyError:
        pytest.fail(
            f"{TEST_SCHEMA_ENV_VAR} must be set by backend/tests/conftest.py under pytest"
        )

    module = _load_no_docs_writes_module()

    captured: dict[str, object] = {}

    def _fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(args=(), returncode=0, stdout="", stderr="")

    # Rebinds only the `subprocess` name inside the freshly-loaded module's
    # own namespace -- never the real, shared subprocess module -- so no
    # other code is affected, and nothing needs manual restoration since
    # `module` is a throwaway object private to this test.
    # setattr rather than plain attribute assignment: mypy types the loader's
    # return as ModuleType, which has no statically-known `subprocess` member.
    setattr(  # noqa: B010
        module,
        "subprocess",
        types.SimpleNamespace(run=_fake_run, CompletedProcess=subprocess.CompletedProcess),
    )

    module.test_evals_suite_leaves_docs_untouched()

    assert "env" in captured, (
        "backend/tests/evals/test_no_docs_writes.py's test_evals_suite_leaves_docs_"
        "untouched() never called subprocess.run"
    )
    child_env = captured["env"]
    assert child_env is not None, (
        "backend/tests/evals/test_no_docs_writes.py spawns its child pytest process "
        "with the default (inherit-everything) environment -- the child ends up "
        f"sharing this process's exact {TEST_SCHEMA_ENV_VAR} value, and its own "
        "teardown then CASCADE-drops that live, shared schema mid-suite."
    )
    # Narrows `object` -> Mapping for the membership check below. Strictly an
    # extra assertion: an env= that is not a mapping is itself a real defect.
    assert isinstance(child_env, Mapping), (
        f"the child env passed to subprocess.run is not a mapping ({type(child_env)!r})"
    )
    assert TEST_SCHEMA_ENV_VAR not in child_env, (
        f"the child env still carries a {TEST_SCHEMA_ENV_VAR} entry "
        f"({child_env.get(TEST_SCHEMA_ENV_VAR)!r}, parent is {parent_schema!r}) -- the "
        "child must derive its own independent schema, not inherit the parent's, or "
        "its own pytest_sessionfinish teardown will DROP SCHEMA CASCADE on it."
    )
