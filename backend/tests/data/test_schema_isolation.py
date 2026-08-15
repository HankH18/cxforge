"""T-16 acceptance 1: per-process Postgres schema isolation.

``data.db.get_connection`` switches ``search_path`` to a private, per-
process schema whenever ``OTHRAM_TEST_SCHEMA`` is set — but that variable
is a test-time-only signal (set exactly once, by ``backend/tests/
conftest.py`` at module-import time; see that file and ``data.db``'s
module docstring). In production nothing ever sets it, so the override
must be structurally inert there, not merely "usually unset by
convention".

``test_get_connection_outside_pytest_uses_the_default_schema`` proves this
the strongest way available to a test process: it launches a genuinely
separate Python subprocess with ``OTHRAM_TEST_SCHEMA`` stripped out of its
environment and, crucially, one that never imports
``backend/tests/conftest.py`` at all (it is invoked via ``python -c``, not
through pytest) — the only file anywhere that ever sets the variable. That
subprocess is as close to "the production code path, outside pytest" as a
test can faithfully simulate, and it must resolve ``current_schema()`` to
``public``, exactly as a real deployment would.

``test_get_connection_outside_pytest_with_override_set_still_uses_default_
schema`` (T-24 acceptance 2) proves the sharper, actually-dangerous case
that test above does not cover: a *leaked* ``OTHRAM_TEST_SCHEMA`` value
present in a non-pytest process's environment. Before T-24, ``data.db.
get_connection`` honored the override wherever it appeared, so a leaked
env var would silently redirect a production process's queries to a test
schema; this is the regression proof that it no longer can, because the
override is now gated on ``PYTEST_VERSION`` (see ``data.db``'s module
docstring for why that signal, and not ``PYTEST_CURRENT_TEST``, was
chosen) also being present, and a bare ``python -c`` subprocess — with
``PYTEST_VERSION`` explicitly stripped, exactly like ``OTHRAM_TEST_SCHEMA``
is stripped in the sibling test — never sets it.

The remaining tests in this module prove the isolation itself works
end-to-end for the CURRENT (test) process: two different
``OTHRAM_TEST_SCHEMA`` values really do land rows in two different,
non-interfering Postgres schemas.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from psycopg import sql

from data.db import TEST_SCHEMA_ENV_VAR, get_connection

pytestmark = pytest.mark.skipif(
    os.getenv("SKIP_DB_TESTS") == "1",
    reason="requires the docker-compose db service (CI sets SKIP_DB_TESTS=1)",
)

# backend/tests/data/test_schema_isolation.py -> data -> tests -> backend -> repo root
REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_SRC = REPO_ROOT / "backend" / "src"

_SUBPROCESS_SNIPPET = (
    "from data.db import get_connection\n"
    "with get_connection() as conn, conn.cursor() as cur:\n"
    "    cur.execute('SELECT current_schema()')\n"
    "    print(cur.fetchone()[0])\n"
)


def test_get_connection_outside_pytest_uses_the_default_schema() -> None:
    """The structural inertness proof STEP 1 requires: a fresh subprocess,
    with the test-time signal removed from its environment and no import
    of the file that ever sets it, must resolve to Postgres's default
    schema (``public``) — proving the override cannot silently leak into
    a production process that never opts in."""
    env = os.environ.copy()
    env.pop(TEST_SCHEMA_ENV_VAR, None)
    # pytest's own [tool.pytest.ini_options] pythonpath (backend/src, .) is
    # a pytest-only mechanism, not inherited by a bare subprocess — set
    # PYTHONPATH explicitly so `from data.db import ...` resolves exactly
    # as it does for every production entry point (main.py, evals/report.py).
    env["PYTHONPATH"] = os.pathsep.join([str(BACKEND_SRC), str(REPO_ROOT)])

    result = subprocess.run(
        [sys.executable, "-c", _SUBPROCESS_SNIPPET],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "public", (
        f"expected the default schema outside pytest, got {result.stdout!r} "
        f"(stderr: {result.stderr!r})"
    )


def test_get_connection_outside_pytest_with_override_set_still_uses_default_schema() -> None:
    """T-24 acceptance 2: the actual leaked-env-var scenario the ticket
    defends against — ``OTHRAM_TEST_SCHEMA`` IS present (deliberately set
    here, not merely absent as in the sibling test above) in a process
    that is not itself pytest. ``PYTEST_VERSION`` is explicitly popped
    (mirroring how the sibling test pops ``OTHRAM_TEST_SCHEMA``) so this
    subprocess is a faithful stand-in for a real production process that
    happens to have inherited a leaked test-schema value from its
    environment: ``get_connection()`` must still resolve to the default
    (``public``) schema, proving the override cannot fire without the
    test-context signal also being present.
    """
    env = os.environ.copy()
    env.pop("PYTEST_VERSION", None)
    env[TEST_SCHEMA_ENV_VAR] = "leaked_schema_should_never_be_used"
    # pytest's own [tool.pytest.ini_options] pythonpath (backend/src, .) is
    # a pytest-only mechanism, not inherited by a bare subprocess — set
    # PYTHONPATH explicitly so `from data.db import ...` resolves exactly
    # as it does for every production entry point (main.py, evals/report.py).
    env["PYTHONPATH"] = os.pathsep.join([str(BACKEND_SRC), str(REPO_ROOT)])

    result = subprocess.run(
        [sys.executable, "-c", _SUBPROCESS_SNIPPET],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "public", (
        f"expected the default schema even with {TEST_SCHEMA_ENV_VAR} leaked into a "
        f"non-pytest process's environment, got {result.stdout!r} (stderr: {result.stderr!r})"
    )


def test_the_current_process_env_var_is_set_and_not_public() -> None:
    """Sanity precondition for the rest of this module: under pytest (this
    process), ``backend/tests/conftest.py`` must have already derived and
    set a non-default schema name before any test runs."""
    schema = os.environ.get(TEST_SCHEMA_ENV_VAR)
    assert schema, f"{TEST_SCHEMA_ENV_VAR} must be set by backend/tests/conftest.py under pytest"
    assert schema != "public"


def test_get_connection_actually_switches_to_that_schema() -> None:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT current_schema()")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == os.environ[TEST_SCHEMA_ENV_VAR]


def test_two_different_schema_values_do_not_see_each_others_rows() -> None:
    """Direct proof of the concurrency guarantee acceptance 5 depends on:
    two ``OTHRAM_TEST_SCHEMA`` values, used one after another through the
    SAME connection factory, land rows in two schemas that cannot see each
    other — the mechanical reason two concurrent full-suite pytest runs
    (two different PIDs, two different derived schema names) cannot
    corrupt each other's data."""
    own_schema = os.environ[TEST_SCHEMA_ENV_VAR]
    other_schema = f"test_isolation_probe_{uuid.uuid4().hex[:8]}"
    marker_value = uuid.uuid4().hex

    try:
        os.environ[TEST_SCHEMA_ENV_VAR] = other_schema
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS isolation_probe (marker text PRIMARY KEY)"
            )
            cur.execute("INSERT INTO isolation_probe (marker) VALUES (%s)", (marker_value,))
    finally:
        os.environ[TEST_SCHEMA_ENV_VAR] = own_schema

    # Back on this test's own schema: the table from the other schema must
    # not be visible under an unqualified name (and if some earlier test
    # ever created a same-named table here, it must not contain the other
    # schema's marker row).
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass(%s)",
            (f"{own_schema}.isolation_probe",),
        )
        own_table = cur.fetchone()
        assert own_table is not None
        if own_table[0] is not None:
            cur.execute("SELECT 1 FROM isolation_probe WHERE marker = %s", (marker_value,))
            assert cur.fetchone() is None, "the other schema's row leaked into this schema"

    # Clean up the probe schema so this test doesn't itself leave litter
    # for the crash-reaper to find.
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(other_schema))
        )
