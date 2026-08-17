"""The job payload, frozen in `docs/DESIGN.md` § *Frozen interface
contracts* §1.1 (ADR-002) and reproduced there byte-identically.

`received_at` is stamped in the **ingress handler** at true webhook receipt
and carried here, so `act` can record the interval DESIGN's § *Latency*
always claimed it recorded ("webhook receipt → public reply posted") rather
than the tail-end Zendesk API calls alone (ADR-004). It rides on the job
payload deliberately: `tickets_seen` keeps its two-column shape (DESIGN
§1.7), because ADR-003 releases the dedup row on failure instead of
tracking run state on it.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class TicketJob(BaseModel):
    ticket_id: str
    comment_id: str
    received_at: datetime  # UTC, stamped in the ingress handler — ADR-004
