"""Shared fixtures for the data-layer test suite.

Every test here needs the docker-compose Postgres; each test module skips
itself via ``pytestmark`` when ``SKIP_DB_TESTS=1`` (set by CI, which has no
db service), mirroring backend/tests/test_bootstrap.py's convention.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from data.seed import DEFAULT_CASES_PATH, SeedResult, seed_all


@pytest.fixture(scope="session")
def seeded() -> SeedResult:
    """Seed once per test session. The suite is still re-runnable from a
    dirty database because ``seed_all`` truncates-and-reloads every call."""
    return seed_all()


@pytest.fixture(scope="session")
def fixture_cases() -> list[dict[str, Any]]:
    """Raw case rows from fixtures/cases.yaml.

    Tests read real fixture content here instead of hardcoding case_ids,
    emails, or stages — that content belongs to the fixture author, not T-1.
    """
    payload = yaml.safe_load(DEFAULT_CASES_PATH.read_text(encoding="utf-8"))
    return list(payload["cases"])
