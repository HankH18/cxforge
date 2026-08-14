"""T-19 acceptance 1-3: ``portal/src/api-types.ts`` is GENERATED from
``backend/src/portal/schemas.py`` by a committed script
(``backend/src/portal/codegen.py``), not hand-written and not verified by a
hand-written parity assertion.

Both tests below run the generator via ``subprocess`` rather than importing
it in-process. That is required, not a style choice: ``conftest.py`` (and
sibling test modules) already do ``from main import app``, so ``main`` is
cached in ``sys.modules`` for this whole pytest process. An in-process
``import`` of a *copied* ``codegen``/``main`` after mutating the copy on
disk would return the cached module and silently ignore the mutation.
Subprocess isolation sidesteps that entirely and, as a bonus, exercises the
exact CLI surface CI actually invokes.

This file needs no database at all — it never imports the FastAPI app in
this process and never touches Postgres. It lives under
``backend/tests/portal/`` purely because that's this ticket's declared file
scope; ``backend/tests/conftest.py``'s ``pytest_collection_modifyitems``
hook (T-16) sweeps every file under this directory by path when
``SKIP_DB_TESTS=1``, so this file is skip-marked locally under that env var
even though it has no DB dependency of its own. Harmless: CI (per T-16's
own ci.yml comment) never sets ``SKIP_DB_TESTS``, and locally a Postgres is
running for the rest of the portal suite regardless.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
CODEGEN = BACKEND_SRC / "portal" / "codegen.py"
API_TYPES_TS = REPO_ROOT / "portal" / "src" / "api-types.ts"


def test_generated_types_are_byte_identical_to_committed() -> None:
    """Acceptance 2: regenerating against the current backend produces
    types byte-identical to what is committed — proven by actually running
    the generator in --check mode, not by a hand comparison.
    """
    result = subprocess.run(
        [sys.executable, str(CODEGEN), "--out", str(API_TYPES_TS), "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"portal/src/api-types.ts is stale relative to backend/src/portal/schemas.py "
        f"-- regenerate with `uv run python backend/src/portal/codegen.py "
        f"--out portal/src/api-types.ts`.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_parity_check_fails_on_a_renamed_backend_field(tmp_path: Path) -> None:
    """Acceptance 3: a deliberate backend field rename FAILS the check,
    demonstrated here rather than by hand. The rename happens against an
    ISOLATED copy of backend/src — the real backend/src/portal/schemas.py
    is never mutated.

    The whole backend/src tree is copied (not just schemas.py) because
    main.py's import chain (ingress, portal, data, helpdesk, escalation,
    agent, ...) must all resolve for `from main import app` to succeed
    inside the copy -- and codegen.py's own sys.path bootstrap
    (Path(__file__).resolve().parents[1]) means the COPIED script naturally
    imports the COPIED tree, never the real one, when the copy is what's
    invoked.

    Renaming a field in schemas.py alone is safe even though the copied
    tree also includes service.py (which constructs
    FeedItem(draft_body=...)): app.openapi() only introspects the class
    definition's type annotations -- it never instantiates FeedItem -- so
    service.py's now-stale keyword argument is irrelevant to schema
    generation.

    Comparing against API_TYPES_TS -- the real, unmodified committed file,
    not a copy of it -- is exactly what proves the check catches drift:
    that's the actual file CI diffs against.
    """
    tmp_backend_src = tmp_path / "backend_src"
    shutil.copytree(BACKEND_SRC, tmp_backend_src, ignore=shutil.ignore_patterns("__pycache__"))

    schemas_copy = tmp_backend_src / "portal" / "schemas.py"
    original = schemas_copy.read_text()
    renamed = original.replace("draft_body: str | None", "body_draft: str | None")
    assert renamed != original, "rename target 'draft_body: str | None' not found in schemas.py"
    schemas_copy.write_text(renamed)

    result = subprocess.run(
        [
            sys.executable,
            str(tmp_backend_src / "portal" / "codegen.py"),
            "--out",
            str(API_TYPES_TS),  # the REAL committed file -- that's the point
            "--check",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0, "the parity check did not catch a renamed backend field"
    assert "draft_body" in result.stderr or "body_draft" in result.stderr, (
        f"expected the diff to mention the renamed field.\nstderr:\n{result.stderr}"
    )
