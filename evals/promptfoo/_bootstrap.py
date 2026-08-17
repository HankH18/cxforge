"""Make promptfoo's Python subprocesses run under THIS repo's interpreter.

promptfoo shells out to whatever ``python`` is first on ``PATH`` for custom
providers, Python assertions, and Python test generators. On a developer machine
that is frequently a system or conda interpreter — measured here on 2026-08-16
it was ``~/opt/anaconda3/bin/python`` (3.9), which cannot even
``from datetime import UTC``, let alone import ``pydantic`` or ``anthropic``.

``PROMPTFOO_PYTHON`` is the documented override, but promptfoo resolves the
``tests:`` block **before** it applies the config's own ``env:`` map, so a
config-level setting does not reach the test generator. Requiring the variable
on the command line would mean plain ``npx promptfoo eval`` — the acceptance
criterion in ``docs/BUILD-PLAN.md §3 Track E`` — does not run.

So: if we are not already on the repo's ``.venv`` interpreter, re-exec into it,
once, guarded by an environment marker so a broken venv can never loop. Then put
``backend/src`` and the repo root on ``sys.path``, exactly as
``evals/report.py`` does for ``uv run python -m evals.report``.

Import this module FIRST in every file promptfoo executes, before any import
that needs a repo dependency.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
_REEXEC_GUARD = "CXFORGE_PROMPTFOO_REEXEC"


def _ensure_repo_interpreter() -> None:
    if os.environ.get(_REEXEC_GUARD):
        # Already re-executed once. If the venv interpreter still cannot import
        # the repo's deps, let the real ImportError surface — silently limping
        # on would produce eval results from a half-loaded module.
        return
    if not _VENV_PYTHON.exists():
        return
    try:
        if Path(sys.executable).resolve() == _VENV_PYTHON.resolve():
            return
    except OSError:  # pragma: no cover - unresolvable executable path
        return
    os.environ[_REEXEC_GUARD] = "1"
    target = str(_VENV_PYTHON)
    # `-u` is what python-shell passes by default; promptfoo's wrapper talks to
    # node over stdout, so keep the stream unbuffered across the re-exec.
    os.execv(target, [target, "-u", *sys.argv])


_ensure_repo_interpreter()

for _path in (str(REPO_ROOT / "backend" / "src"), str(REPO_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)
