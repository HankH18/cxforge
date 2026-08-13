"""T-1 acceptance 3: typed case lookups, hit/miss, and requester fan-out.

Also covers "every stage value in the fixture round-trips through the
enum" via test_every_fixture_stage_round_trips_through_the_enum, which reads
every seeded case back through get_case and checks the stage survived the
Postgres enum + Pydantic Literal round trip unchanged.
"""

from __future__ import annotations

import os
from collections import Counter
from typing import Any

import pytest

from data.lookup import get_case, get_cases_by_requester
from data.models import STAGES, Case, CaseNotFound
from data.seed import SeedResult

pytestmark = pytest.mark.skipif(
    os.getenv("SKIP_DB_TESTS") == "1",
    reason="requires the docker-compose db service (CI sets SKIP_DB_TESTS=1)",
)


def test_get_case_hit_returns_correct_typed_fields(
    seeded: SeedResult, fixture_cases: list[dict[str, Any]]
) -> None:
    expected = fixture_cases[0]

    result = get_case(expected["case_id"])

    assert isinstance(result, Case)
    assert result.case_id == expected["case_id"]
    assert result.requester_email == expected["requester_email"]
    assert result.requester_name == expected.get("requester_name")
    assert result.stage == expected["stage"]
    assert str(result.stage_entered_at) == expected["stage_entered_at"]
    assert str(result.last_updated) == expected["last_updated"]
    assert result.eta_weeks == expected["eta_weeks"]
    assert result.dna_profile_available == expected["dna_profile_available"]
    assert result.photos_available == expected["photos_available"]


def test_get_case_miss_returns_typed_not_found_not_none_not_exception(
    seeded: SeedResult,
) -> None:
    result = get_case("case-does-not-exist-xyz")

    # A typed sentinel, not None and not (as a bare `except Exception` might
    # imply) an exception the caller had to catch.
    assert result is not None
    assert isinstance(result, CaseNotFound)
    assert not isinstance(result, Case)
    assert result.case_id == "case-does-not-exist-xyz"


def test_get_cases_by_requester_returns_all_cases_for_multi_case_requester(
    seeded: SeedResult, fixture_cases: list[dict[str, Any]]
) -> None:
    email_counts = Counter(row["requester_email"] for row in fixture_cases)
    multi_case_email = next(email for email, count in email_counts.items() if count > 1)

    results = get_cases_by_requester(multi_case_email)

    assert len(results) > 1
    assert all(isinstance(r, Case) for r in results)
    assert all(r.requester_email == multi_case_email for r in results)
    expected_ids = {
        row["case_id"] for row in fixture_cases if row["requester_email"] == multi_case_email
    }
    assert {r.case_id for r in results} == expected_ids


def test_get_cases_by_requester_miss_returns_empty_typed_list(seeded: SeedResult) -> None:
    results = get_cases_by_requester("nobody@nowhere.example")
    assert results == []


def test_every_fixture_stage_round_trips_through_the_enum(
    seeded: SeedResult, fixture_cases: list[dict[str, Any]]
) -> None:
    stages_in_fixture = {row["stage"] for row in fixture_cases}
    assert stages_in_fixture == set(STAGES), "fixture must cover every stage"

    for row in fixture_cases:
        result = get_case(row["case_id"])
        assert isinstance(result, Case)
        assert result.stage == row["stage"]
