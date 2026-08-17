"""W1-F — the deploy bootstrap must not truncate a live knowledge base.

`data.seed.seed_all` begins with ``TRUNCATE TABLE cases`` and ``TRUNCATE TABLE
kb_chunks``. `deploy/backend/entrypoint.sh` runs `bootstrap.py` on **every**
container start, and both app services carry ``restart: unless-stopped``.
A compose ``depends_on`` condition orders ``up`` and nothing else — not a
restart, not the daemon bringing containers back after a reboot.

So a backend crash-and-restart used to reload the KB underneath a worker that
was already consuming jobs. The observable symptom is an ungrounded reply or a
retrieval failure, with nothing in any log connecting it to the restart, which
is why it is worth a test rather than a comment.

Moving the worker off the entrypoint (`deploy/docker-compose.yml`) removed the
*second* seeder. It did not remove this hazard — the hazard was never about
two seeders — and the comment there previously implied otherwise.

The two subprocess tests are the ones that matter. `should_seed` returning
False proves a decision; only running `main()` proves the decision is actually
consulted before `seed_all` is called. The child gets a stub `data` package on
its path, so nothing here touches Postgres — important while other agents are
running against the same `othram-db`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BOOTSTRAP = REPO_ROOT / "deploy" / "backend" / "bootstrap.py"

# Imported by path rather than by `import bootstrap`, both because
# deploy/backend is not on sys.path and because a first-party import statement
# from a test suite directory is a graph edge nobody needs.
_SPEC_SOURCE = BOOTSTRAP.read_text()


def _bootstrap_module() -> object:
    """Load the pure decision helpers without importing `data`.

    `bootstrap.py` imports `data.db`/`data.schema` at module scope, so the
    module is exec'd with a stub `data` package already installed in
    `sys.modules` for the duration.
    """
    import importlib.util
    import types

    stub_pkg = types.ModuleType("data")
    stub_pkg.__path__ = []
    stub_db = types.ModuleType("data.db")
    stub_db.get_connection = lambda: None  # type: ignore[attr-defined]
    stub_schema = types.ModuleType("data.schema")
    stub_schema.init_schema = lambda conn: None  # type: ignore[attr-defined]

    saved = {k: sys.modules.get(k) for k in ("data", "data.db", "data.schema")}
    sys.modules.update({"data": stub_pkg, "data.db": stub_db, "data.schema": stub_schema})
    try:
        spec = importlib.util.spec_from_file_location("_deploy_bootstrap", BOOTSTRAP)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in saved.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value


# --------------------------------------------------------------------------
# The decision
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "if-empty"),
        ("", "if-empty"),
        ("true", "if-empty"),
        ("TRUE", "if-empty"),
        ("yes", "if-empty"),
        ("false", "never"),
        ("FALSE", "never"),
        ("0", "never"),
        ("off", "never"),
        ("force", "force"),
        ("FORCE", "force"),
        # A typo must never be able to truncate a populated KB.
        ("ture", "if-empty"),
        ("forse", "if-empty"),
    ],
)
def test_seed_mode_parsing(raw: str | None, expected: str) -> None:
    module = _bootstrap_module()
    assert module.seed_mode(raw) == expected  # type: ignore[attr-defined]


def test_a_populated_database_is_never_reseeded_by_default() -> None:
    """The whole point: `true` (and unset) stops being destructive."""
    module = _bootstrap_module()
    should_seed = module.should_seed  # type: ignore[attr-defined]

    assert should_seed("if-empty", case_count=30, kb_chunk_count=44) is False
    assert should_seed("if-empty", case_count=0, kb_chunk_count=44) is False
    assert should_seed("if-empty", case_count=30, kb_chunk_count=0) is False
    # A genuinely empty database still gets seeded, so a fresh deploy is
    # demo-able on first boot exactly as before.
    assert should_seed("if-empty", case_count=0, kb_chunk_count=0) is True
    # The deliberate escape hatch, and the off switch.
    assert should_seed("force", case_count=30, kb_chunk_count=44) is True
    assert should_seed("never", case_count=0, kb_chunk_count=0) is False


# --------------------------------------------------------------------------
# The decision, actually consulted
# --------------------------------------------------------------------------

_STUB_DATA_PKG = '''
import json, os, pathlib
_MARKER = pathlib.Path(os.environ["BOOTSTRAP_MARKER"])
_COUNTS = json.loads(os.environ["BOOTSTRAP_COUNTS"])


def _record(event):
    events = json.loads(_MARKER.read_text()) if _MARKER.exists() else []
    events.append(event)
    _MARKER.write_text(json.dumps(events))
'''

_STUB_DB = '''
import contextlib
from data import _COUNTS, _record


class _Cursor:
    def execute(self, sql, *a, **kw):
        _record({"event": "execute", "sql": " ".join(sql.split())})

    def fetchone(self):
        return (_COUNTS["cases"], _COUNTS["kb_chunks"])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def cursor(self):
        return _Cursor()


@contextlib.contextmanager
def get_connection():
    _record({"event": "get_connection"})
    yield _Conn()
'''

_STUB_SCHEMA = '''
from data import _record


def init_schema(conn):
    _record({"event": "init_schema"})
'''

_STUB_SEED = '''
from dataclasses import dataclass
from data import _record


@dataclass
class SeedResult:
    case_count: int
    kb_chunk_count: int


def seed_all():
    _record({"event": "seed_all"})
    return SeedResult(case_count=30, kb_chunk_count=44)
'''


def _run_bootstrap(
    tmp_path: Path, *, seed_on_start: str | None, cases: int, kb_chunks: int
) -> list[dict[str, str]]:
    """Run the real bootstrap.py against a stub `data` package. No Postgres."""
    stub_root = tmp_path / "stub"
    pkg = stub_root / "data"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(_STUB_DATA_PKG)
    (pkg / "db.py").write_text(_STUB_DB)
    (pkg / "schema.py").write_text(_STUB_SCHEMA)
    (pkg / "seed.py").write_text(_STUB_SEED)

    marker = tmp_path / "events.json"
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(stub_root),
        "BOOTSTRAP_MARKER": str(marker),
        "BOOTSTRAP_COUNTS": json.dumps({"cases": cases, "kb_chunks": kb_chunks}),
    }
    if seed_on_start is not None:
        env["SEED_ON_START"] = seed_on_start

    result = subprocess.run(
        [sys.executable, str(BOOTSTRAP)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    events = json.loads(marker.read_text())
    assert isinstance(events, list)
    # Every path must ensure the schema exists — that is this file's original
    # reason to exist and must survive the seeding change.
    assert any(e["event"] == "init_schema" for e in events), events
    return events


def test_running_the_bootstrap_over_a_populated_database_does_not_seed(
    tmp_path: Path,
) -> None:
    """The regression that matters, exercised end to end through `main()`."""
    events = _run_bootstrap(tmp_path, seed_on_start="true", cases=30, kb_chunks=44)
    assert not any(e["event"] == "seed_all" for e in events), (
        "bootstrap.py called seed_all() against a populated database. "
        "seed_all TRUNCATEs cases and kb_chunks, and this runs on every "
        "container restart while the worker is consuming jobs."
    )


def test_running_the_bootstrap_over_an_empty_database_still_seeds(
    tmp_path: Path,
) -> None:
    """A fresh deploy must stay demo-able on first boot."""
    events = _run_bootstrap(tmp_path, seed_on_start=None, cases=0, kb_chunks=0)
    assert any(e["event"] == "seed_all" for e in events), events


def test_force_reseeds_a_populated_database(tmp_path: Path) -> None:
    events = _run_bootstrap(tmp_path, seed_on_start="force", cases=30, kb_chunks=44)
    assert any(e["event"] == "seed_all" for e in events), events


def test_false_creates_the_schema_and_never_counts_or_seeds(tmp_path: Path) -> None:
    """`false` keeps its documented meaning exactly — docs/deploy.md relies on it."""
    events = _run_bootstrap(tmp_path, seed_on_start="false", cases=0, kb_chunks=0)
    assert not any(e["event"] == "seed_all" for e in events), events
    assert not any(e["event"] == "execute" for e in events), (
        "SEED_ON_START=false should not even query the content tables"
    )
