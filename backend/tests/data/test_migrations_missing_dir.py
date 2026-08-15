"""T-30 acceptance 3: a missing (or empty) ``migrations/`` directory must
abort schema init loudly, instead of ``discover_migrations`` silently
returning ``[]`` and letting the caller boot "successfully" against a
database with zero tables applied.

Pre-T-30, ``Path.glob("*.sql")`` on a directory that does not exist behaves
like a no-match shell glob rather than raising (confirmed by hand: it
returns an empty iterator, not an ``OSError``) -- so a stripped deployment
image that never copied ``backend/src/data/migrations/`` at all would boot
clean and only fail much later, confusingly, as a "relation does not
exist" error the first time a route touched a table only a migration
creates.

Every test here is fully hermetic: it points ``data.schema.MIGRATIONS_DIR``
at a ``tmp_path``-derived location via ``monkeypatch.setattr`` and restores
it automatically at teardown. None of them touch, delete, or move the real
``backend/src/data/migrations/`` directory, and none of them need the
docker-compose Postgres service -- ``discover_migrations`` and the
directory-existence guard in ``init_schema`` are pure filesystem/Python
logic, so these tests need no ``SKIP_DB_TESTS`` guard and no db fixture.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

import data.schema as schema


class _FakeCursor:
    """Minimal stand-in for a ``psycopg`` cursor: records nothing, just
    accepts ``execute`` calls and supports the context-manager protocol
    ``init_schema`` uses it with. No real SQL is ever sent -- these tests
    only need to prove ``init_schema`` never reaches its final
    ``conn.commit()`` when ``discover_migrations`` raises, not that its
    DDL is correct (that is proven elsewhere, against real Postgres, by
    ``backend/tests/data/test_migrations.py``)."""

    def execute(self, statement: str, params: object = None) -> None:
        pass

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc_info: object) -> Literal[False]:
        return False


class _FakeConnection:
    """Minimal stand-in for a ``psycopg.Connection``: hands out
    ``_FakeCursor``s and records whether ``commit`` was ever called, so a
    test can assert ``init_schema`` aborted BEFORE reaching its
    success-path commit."""

    def __init__(self) -> None:
        self.committed = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor()

    def commit(self) -> None:
        self.committed = True


# ---------------------------------------------------------------------------
# The stripped-image case: migrations/ does not exist at all.
# ---------------------------------------------------------------------------


def test_discover_migrations_raises_loudly_when_directory_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing_dir = tmp_path / "migrations"
    assert not missing_dir.exists(), "precondition: the probe path must not exist"
    monkeypatch.setattr(schema, "MIGRATIONS_DIR", missing_dir)

    with pytest.raises(schema.MigrationsDirectoryError) as excinfo:
        schema.discover_migrations()

    assert str(missing_dir) in str(excinfo.value), (
        "the exception message must name the expected (missing) path so the "
        "failure is actionable, not just 'something is wrong'"
    )


def test_init_schema_aborts_before_commit_when_directory_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loud failure must land at schema-init time, not just inside
    ``discover_migrations`` in isolation -- a caller running the real
    deploy-time bootstrap (``deploy/backend/bootstrap.py`` ->
    ``init_schema(conn)``) must see this abort before anything is treated
    as done."""
    missing_dir = tmp_path / "migrations"
    monkeypatch.setattr(schema, "MIGRATIONS_DIR", missing_dir)
    conn = _FakeConnection()

    with pytest.raises(schema.MigrationsDirectoryError):
        schema.init_schema(conn)  # type: ignore[arg-type]

    assert conn.committed is False, (
        "init_schema must abort before its final conn.commit() when the "
        "migrations directory is missing -- never report success"
    )


# ---------------------------------------------------------------------------
# The complementary case: migrations/ exists but ships zero *.sql files.
#
# This is a genuinely different condition from "the directory is absent"
# (e.g. a directory that survives on a live filesystem but whose contents
# were excluded by a build step), and it is equally broken in a real image:
# this project has shipped migration 0001 since T-20, so an image with a
# present-but-empty migrations/ has just as surely lost real migrations as
# one missing the directory outright. discover_migrations() therefore
# raises the same exception for both -- see its docstring/module docstring
# in data/schema.py for why the two are treated identically rather than
# only the absent-directory case being guarded.
# ---------------------------------------------------------------------------


def test_discover_migrations_raises_loudly_when_directory_is_present_but_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_dir = tmp_path / "migrations"
    empty_dir.mkdir()
    assert list(empty_dir.iterdir()) == [], "precondition: the probe dir must be empty"
    monkeypatch.setattr(schema, "MIGRATIONS_DIR", empty_dir)

    with pytest.raises(schema.MigrationsDirectoryError) as excinfo:
        schema.discover_migrations()

    assert str(empty_dir) in str(excinfo.value), (
        "the exception message must name the expected (empty) path so the "
        "failure is actionable, not just 'something is wrong'"
    )


def test_discover_migrations_raises_loudly_even_with_a_non_sql_file_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty migrations/ is not proven merely by an empty directory --
    a directory that exists and has files in it, but none of them ``*.sql``,
    must be treated identically: zero migrations would still be applied."""
    dir_with_stray_file = tmp_path / "migrations"
    dir_with_stray_file.mkdir()
    (dir_with_stray_file / "README.md").write_text("not a migration\n", encoding="utf-8")
    monkeypatch.setattr(schema, "MIGRATIONS_DIR", dir_with_stray_file)

    with pytest.raises(schema.MigrationsDirectoryError):
        schema.discover_migrations()


# ---------------------------------------------------------------------------
# Sanity: the guard is specific to "missing or empty" -- a directory that
# legitimately contains a migration file must still work exactly as before.
# ---------------------------------------------------------------------------


def test_discover_migrations_still_succeeds_against_a_populated_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    populated_dir = tmp_path / "migrations"
    populated_dir.mkdir()
    (populated_dir / "0001_probe.sql").write_text("SELECT 1;", encoding="utf-8")
    monkeypatch.setattr(schema, "MIGRATIONS_DIR", populated_dir)

    migrations = schema.discover_migrations()

    assert [m.version for m in migrations] == [1]
    assert migrations[0].name == "probe"
    assert migrations[0].sql == "SELECT 1;"
