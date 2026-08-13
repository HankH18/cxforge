"""Typed case lookups: by case_id and by requester_email.

A miss on ``get_case`` returns ``CaseNotFound`` — never ``None``, never a
raised exception — so a caller (the agent's case-status tool, T-5) is forced
by the type system to branch on the miss rather than silently treating an
absent case as falsy-and-ignorable or crashing the run mid-reply.
"""

from __future__ import annotations

from typing import Any

from data.db import get_connection
from data.models import Case, CaseNotFound

_COLUMNS = (
    "case_id, requester_email, requester_name, stage, stage_entered_at, "
    "last_updated, eta_weeks, dna_profile_available, photos_available"
)


def _row_to_case(row: tuple[Any, ...]) -> Case:
    return Case(
        case_id=row[0],
        requester_email=row[1],
        requester_name=row[2],
        stage=row[3],
        stage_entered_at=row[4],
        last_updated=row[5],
        eta_weeks=row[6],
        dna_profile_available=row[7],
        photos_available=row[8],
    )


def get_case(case_id: str) -> Case | CaseNotFound:
    """Look up a case by its case_id. A miss returns ``CaseNotFound``."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {_COLUMNS} FROM cases WHERE case_id = %s", (case_id,))
        row = cur.fetchone()
    if row is None:
        return CaseNotFound(case_id=case_id)
    return _row_to_case(row)


def get_cases_by_requester(email: str) -> list[Case]:
    """Look up every case for a requester email. A miss returns ``[]``."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLUMNS} FROM cases WHERE requester_email = %s ORDER BY case_id",
            (email,),
        )
        rows = cur.fetchall()
    return [_row_to_case(row) for row in rows]
