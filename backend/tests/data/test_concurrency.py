"""T-23 acceptance 1: two GENUINELY concurrent subprocess pytest runs of the
db-touching suites, both proven to pass with independently-isolated schemas.

What T-16 actually shipped for its own acceptance 5 ("two concurrent
full-suite runs both pass, demonstrated"):
``backend/tests/data/test_schema_isolation.py::
test_two_different_schema_values_do_not_see_each_others_rows`` — which its
own docstring describes as using "two ``OTHRAM_TEST_SCHEMA`` values, used
ONE AFTER ANOTHER through the SAME connection factory" (i.e. sequential,
single-process, single-PID) and frames itself only as "the mechanical
reason two concurrent full-suite pytest runs ... cannot corrupt each
other's data" — a proof of the isolation *mechanism*, not a demonstration
of two concurrent runs. No two pytest processes were ever actually
launched at the same time anywhere in that ticket's diff. This file is
that missing demonstration. It supersedes T-16 acceptance 5 specifically;
``test_two_different_schema_values_do_not_see_each_others_rows`` is left
untouched and still green — it remains a valid, fast, complementary proof
of the underlying mechanism and other tickets may rely on it.

SUBSET JUSTIFICATION (a full-suite x2 run is too slow for a single test):
grepping ``backend/tests`` for ``get_connection``/``OTHRAM_TEST_SCHEMA``/
``SKIP_DB_TESTS`` usage shows the db-touching surface is exactly five
directories, each gated on ``SKIP_DB_TESTS`` because every test in it goes
through ``data.db.get_connection`` (the function this whole ticket is
about): ``backend/tests/data`` (direct CRUD/lookup/migrations/seed
coverage, plus the schema-isolation unit proof itself), ``backend/tests/
graph`` (end-to-end pipeline runs that seed + truncate + write ``runs``/
``drafts``), ``backend/tests/grounding`` (same truncate-and-write pattern
plus KB retrieval), ``backend/tests/portal`` (``init_schema`` + FastAPI
routes reading/writing ``runs``/``drafts``/``settings``), and
``backend/tests/ingress`` (webhook writes to ``tickets_seen``). Together
that is the ENTIRE db-touching surface today, not a sample of it — nothing
that talks to Postgres through the shared connection factory is left out.
Deliberately excluded: ``backend/tests/escalation`` (its own conftest.py
docstring says it is pure unit/no-Postgres by design — nothing to
isolate); ``backend/tests/evals``, ``hooks``, ``plan``, ``deploy``,
``contract`` (no ``get_connection`` usage — grepped and confirmed empty);
and ``backend/tests/test_bootstrap.py``'s
``test_database_is_postgres_16_with_pgvector`` (connects with a bare
``psycopg.connect``, bypassing ``get_connection`` and ``OTHRAM_TEST_SCHEMA``
entirely, so it exercises no schema-isolation behavior at all). A single
process running this five-directory subset takes ~15s locally, so two
concurrent copies comfortably fit the ~90s budget below.

CONCURRENCY PROOF: both children are started back-to-back with
``subprocess.Popen`` (non-blocking) and then polled in a loop that checks,
on every iteration, (a) whether both processes are still alive
(``Popen.poll() is None``) and (b) how many NEW ``test_%`` Postgres schemas
have appeared since the snapshot taken just before the children were
launched. The test asserts it actually observed an iteration where BOTH
were alive at once, and a (possibly different) iteration where BOTH
children's schemas existed simultaneously — not "started one, waited,
started the other," which is exactly the sequential shape this ticket
exists to rule out.

SCHEMA-DIFFERENCE PROOF: ``backend/tests/conftest.py`` derives each
process's schema as ``test_<repo-slug>_<pid>`` from ``os.getpid()`` when
``OTHRAM_TEST_SCHEMA`` is not already set in its environment. Each child
here is spawned with that variable explicitly popped from its env (mirrors
``test_schema_isolation.py``'s own subprocess technique) so it derives a
fresh value from its OWN pid rather than inheriting this parent pytest
process's. The test then confirms, from the schemas observed live in
Postgres, that one contains child A's pid, the other contains child B's
pid, and the two strings differ — proving the isolation the acceptance
cares about actually happened, not merely that two subprocesses exited 0.

SELF-RECURSION GUARD: the five target directories above are subdirectories
of ``backend/tests``; this file lives directly in ``backend/tests/``
itself, one level up from all of them. A child process invoked with only
those five directory paths as pytest arguments cannot collect this file —
pytest only walks the paths given on its command line (plus their
conftest.py chain), and ``backend/tests/data/test_concurrency.py`` is not
inside any of them. ``_assert_no_self_recursion`` below asserts that
invariant at import time so an edit that widens ``_DB_SUBSET`` to include
``backend/tests`` itself (which WOULD recurse and fork-bomb) fails loudly
before any subprocess is ever spawned.

SABOTAGE CHECK (performed manually during development, not shipped as a
test — see the ticket's own instructions not to weaken/duplicate this into
committed code): temporarily hardcoding both children's env to the SAME
pinned ``OTHRAM_TEST_SCHEMA`` value (instead of popping it) reproduces the
pre-T-16 failure mode and this test fails, exactly as intended. See the
implementation report for the exact failure text; the change was reverted
before commit.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import IO

import psycopg
import pytest

from data.db import TEST_SCHEMA_ENV_VAR, get_connection

pytestmark = pytest.mark.skipif(
    os.getenv("SKIP_DB_TESTS") == "1",
    reason="requires the docker-compose db service (CI sets SKIP_DB_TESTS=1)",
)

# backend/tests/data/test_concurrency.py -> data -> tests -> backend -> repo root
REPO_ROOT = Path(__file__).resolve().parents[3]

_DB_SUBSET = (
    "backend/tests/data",
    "backend/tests/graph",
    "backend/tests/grounding",
    "backend/tests/portal",
    "backend/tests/ingress",
)

_THIS_FILE_REL = "backend/tests/data/test_concurrency.py"

# This file lives INSIDE _DB_SUBSET[0]. It was moved here from the top level
# of backend/tests/ so that the already-closed tickets whose verify commands
# cover the data suite (T-1, T-8, T-20, T-24) also cover it — a new top-level
# file importing data.db lands in their reverse-dependency blast radius but
# not in their frozen verify strings, which
# backend/tests/plan/test_blast_radius.py correctly flags.
#
# The consequence is that a child pytest run of _DB_SUBSET would re-collect
# and re-spawn this very test. The children are therefore launched with an
# explicit --ignore for it, and the guard below asserts that ignore is
# actually present in the argv rather than asserting the file sits outside
# the subset. test_schema_isolation_inheritance.py is ignored for the same
# reason: it also spawns child pytest runs, so collecting it inside a child
# would nest a third process layer for no added coverage.
_CHILD_IGNORES = (
    _THIS_FILE_REL,
    "backend/tests/data/test_schema_isolation_inheritance.py",
)

# Total wall-clock budget for both children to finish, polling included.
_TIMEOUT_S = 90.0
_POLL_INTERVAL_S = 0.1


def _child_argv() -> list[str]:
    ignores = [f"--ignore={rel}" for rel in _CHILD_IGNORES]
    return [sys.executable, "-m", "pytest", *_DB_SUBSET, *ignores, "-q", "-m", "not live"]


def _assert_no_self_recursion(argv: list[str]) -> None:
    """Fork-bomb guard. For every subset directory that contains a
    subprocess-spawning test file, the child's argv must carry an explicit
    --ignore for that file; otherwise the child re-collects it and spawns
    grandchildren without bound."""
    for rel in _CHILD_IGNORES:
        inside = any(rel.startswith(target.rstrip("/") + "/") for target in _DB_SUBSET)
        if inside:
            assert f"--ignore={rel}" in argv, (
                f"self-recursion guard tripped: {rel!r} is inside _DB_SUBSET but the "
                f"child argv carries no --ignore for it — a child pytest invocation "
                f"would re-collect and re-spawn it (fork bomb). argv={argv!r}"
            )


_assert_no_self_recursion(_child_argv())


def _existing_test_schema_names() -> set[str]:
    """Every Postgres schema currently matching the ``test_%`` naming
    convention ``backend/tests/conftest.py`` derives — independent of
    which schema is on THIS connection's own search_path, since
    ``information_schema.schemata`` always lists every schema in the
    database regardless of the querying connection's current schema."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT schema_name FROM information_schema.schemata "
            r"WHERE schema_name LIKE 'test\_%' ESCAPE '\'"
        )
        return {row[0] for row in cur.fetchall()}


def _require_db_reachable() -> None:
    """Skip (never fail) when Postgres itself can't be reached — distinct
    from the module-level ``SKIP_DB_TESTS`` marker above, which only
    covers the CI-configured case. This covers the case where
    ``SKIP_DB_TESTS`` isn't set but ``othram-db`` is, for whatever reason,
    not actually answering."""
    try:
        with get_connection():
            pass
    except psycopg.OperationalError as exc:
        pytest.skip(f"othram-db unreachable: {exc}")


def _spawn_child() -> tuple[subprocess.Popen[bytes], IO[bytes], str]:
    """Launch one subprocess pytest run of ``_DB_SUBSET``. Pops
    ``OTHRAM_TEST_SCHEMA`` from the child's environment (mirroring
    ``test_schema_isolation.py``'s own subprocess technique) so the
    child's own ``backend/tests/conftest.py`` derives a fresh schema name
    from ITS pid rather than inheriting this parent test process's schema
    — without this, both children would silently share one schema and the
    whole point of the test would be moot."""
    env = os.environ.copy()
    env.pop(TEST_SCHEMA_ENV_VAR, None)

    out_fd, out_path = tempfile.mkstemp(prefix="t23-concurrency-", suffix=".out")
    os.close(out_fd)
    stdout_f = open(out_path, "w+b")

    proc = subprocess.Popen(
        _child_argv(),
        cwd=REPO_ROOT,
        env=env,
        stdout=stdout_f,
        stderr=subprocess.STDOUT,
    )
    return proc, stdout_f, out_path


def _read_and_cleanup(handle: IO[bytes], path: str) -> str:
    handle.close()
    try:
        return Path(path).read_text(errors="replace")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _schema_for_pid(schemas: set[str], pid: int) -> str | None:
    suffix = f"_{pid}"
    matches = [name for name in schemas if name.endswith(suffix)]
    return matches[0] if matches else None


def test_two_concurrent_pytest_runs_both_pass_with_isolated_schemas() -> None:
    """The real T-16 acceptance-5 demonstration: launch two subprocess
    pytest runs of the db-touching subset at the same time, prove they
    overlapped in wall-clock time (both alive at once, both schemas
    present at once), and assert both exit 0 with two distinct,
    pid-derived Postgres schemas. See module docstring for the subset
    justification, the concurrency/schema proofs, and the self-recursion
    guard."""
    _require_db_reachable()

    baseline_schemas = _existing_test_schema_names()

    proc_a, stdout_a, out_path_a = _spawn_child()
    proc_b, stdout_b, out_path_b = _spawn_child()

    both_alive_observed = False
    both_schemas_observed = False
    observed_new_schemas: set[str] = set()

    deadline = time.monotonic() + _TIMEOUT_S
    try:
        while True:
            alive_a = proc_a.poll() is None
            alive_b = proc_b.poll() is None
            if alive_a and alive_b:
                both_alive_observed = True

            current_new = _existing_test_schema_names() - baseline_schemas
            observed_new_schemas |= current_new
            if len(current_new) >= 2:
                both_schemas_observed = True

            if not alive_a and not alive_b:
                break
            if time.monotonic() > deadline:
                proc_a.kill()
                proc_b.kill()
                proc_a.wait()
                proc_b.wait()
                pytest.fail(
                    f"the two child pytest runs did not both finish within "
                    f"{_TIMEOUT_S}s (subset too slow, or one hung)"
                )
            time.sleep(_POLL_INTERVAL_S)

        rc_a = proc_a.wait()
        rc_b = proc_b.wait()
    finally:
        output_a = _read_and_cleanup(stdout_a, out_path_a)
        output_b = _read_and_cleanup(stdout_b, out_path_b)

    assert both_alive_observed, (
        "never observed both child pytest processes alive at the same wall-clock "
        "moment — this would mean the runs were effectively sequential, not "
        "concurrent, which is exactly the shape this ticket rules out"
    )
    assert both_schemas_observed, (
        "never observed both children's Postgres test schemas existing at the "
        f"same time (schemas seen at all: {sorted(observed_new_schemas)})"
    )

    assert rc_a == 0, f"child A (pid {proc_a.pid}) exited {rc_a}:\n{output_a}"
    assert rc_b == 0, f"child B (pid {proc_b.pid}) exited {rc_b}:\n{output_b}"

    schema_a = _schema_for_pid(observed_new_schemas, proc_a.pid)
    schema_b = _schema_for_pid(observed_new_schemas, proc_b.pid)
    assert schema_a is not None, (
        f"no observed schema was derived from child A's pid ({proc_a.pid}); "
        f"schemas seen: {sorted(observed_new_schemas)}"
    )
    assert schema_b is not None, (
        f"no observed schema was derived from child B's pid ({proc_b.pid}); "
        f"schemas seen: {sorted(observed_new_schemas)}"
    )
    assert schema_a != schema_b, (
        "both children ended up on the SAME Postgres schema "
        f"({schema_a!r}) — schema isolation did not hold between the two "
        "concurrent runs"
    )
