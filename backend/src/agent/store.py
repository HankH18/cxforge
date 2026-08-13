"""Thin persistence helpers over the ``settings``/``runs``/``drafts`` tables
T-1 already created (``backend/src/data/schema.py``'s own docstring:
"T-1 is the only ticket with backend/src/data/** in scope, so every table
later tickets need — runs (T-5/T-6); drafts/settings (T-8) — is created
now, up front. Later tickets read/write those tables through
get_connection without touching this package.") T-5 adds no schema here —
only reads/writes rows through ``data.get_connection``, exactly as that
docstring prescribes, for exactly the two things ``decide``/``act`` need:
reading the gate, and recording a run (plus, when gated, its pending
draft).

``GATE_SETTING_KEY`` (``agent.config``) is the ``settings.key`` read here.
T-8 owns *writing* it (``PUT /api/settings/gate``); this module only reads,
defaulting to OFF (R11's documented default) when no row exists yet.
"""

from __future__ import annotations

from datetime import datetime

from agent.config import GATE_SETTING_KEY
from data import get_connection
from escalation.schemas import Reason

_TRUE_VALUES = {"1", "true", "on", "yes"}


def read_gate_enabled() -> bool:
    """Read R11's boolean send/hold gate. Defaults to ``False`` (OFF) when
    the ``settings`` row doesn't exist yet — R11: "OFF (default): autonomous
    send." """
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT value FROM settings WHERE key = %s", (GATE_SETTING_KEY,))
        row = cur.fetchone()
    if row is None or row[0] is None:
        return False
    return row[0].strip().lower() in _TRUE_VALUES


def record_run(
    *,
    ticket_id: str,
    route: str | None,
    confidence: float | None,
    outcome: str | None,
    verifier_score: float | None,
    trace_id: str,
    received_at: datetime,
    replied_at: datetime | None,
    reasons: list[Reason] | None = None,
) -> int:
    """Insert one ``runs`` row (R10's portal feed, R13's metrics source).
    ``outcome`` is left ``None`` when the gate holds the draft pending —
    T-8's approve/reject flow fills it in once a human decides.

    ``reasons`` is the escalation decision's own ``EscalationTrigger.reason``
    list (``agent.nodes.act`` passes ``state["escalation"].triggers``'
    reasons here) — every reason DESIGN's combinator attached to THIS run,
    in the order the engine produced them, duplicates already removed
    upstream by ``escalation.engine``'s own dedupe. Left ``None``/empty
    (stored as ``'{}'``, ``data.schema``'s column default) for a run that
    never escalated — never a fake placeholder reason. ``portal.service.
    compute_metrics``'s ``escalations_by_reason`` and ``fetch_feed``'s
    ``escalation_reason`` are this column's only readers."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO runs (
                ticket_id, route, confidence, outcome, verifier_score,
                trace_id, received_at, replied_at, reasons
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                ticket_id,
                route,
                confidence,
                outcome,
                verifier_score,
                trace_id,
                received_at,
                replied_at,
                list(reasons) if reasons else [],
            ),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("INSERT ... RETURNING id produced no row")
    return int(row[0])


def record_draft(*, run_id: int, body: str, status: str) -> int:
    """Insert one ``drafts`` row linked to ``run_id``. ``status`` is
    ``"pending"`` when the gate held the draft for review, or
    ``"auto_sent"`` when ``act`` sent it immediately (``draft_enum``'s two
    values a graph run itself can produce — ``"approved"``/``"rejected"``
    are T-8's portal-driven transitions)."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO drafts (run_id, body, status) VALUES (%s, %s, %s) RETURNING id",
            (run_id, body, status),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("INSERT ... RETURNING id produced no row")
    return int(row[0])
