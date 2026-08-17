#!/usr/bin/env python3
"""Manual live smoke test for ZendeskAdapter against a real Zendesk trial.

Exercises every HelpdeskPort operation once, in sequence, against whatever
ticket ``ZENDESK_*`` credentials in the environment give access to. This is
a human-run tool (docs/tickets.json T-2) — never invoked by CI or by the
`-m contract` suite, which runs entirely over mocked HTTP instead.

Usage:
    uv run python scripts/live_smoke.py <ticket_id>

Env (see .env.example, filled in by following docs/zendesk-runbook.md):
    ZENDESK_SUBDOMAIN, ZENDESK_OAUTH_TOKEN   required
    ZENDESK_AI_USER_ID                        optional, improves author_kind mapping
    ZENDESK_LIVE_SMOKE_GROUP_ID                optional, exercises assign_group if set

If ZENDESK_SUBDOMAIN or ZENDESK_OAUTH_TOKEN is absent, this prints why and
exits 0 immediately — before importing anything that could construct an
HTTP client — rather than attempting any network call. That's the expected
state in this build environment: no Zendesk trial exists here yet.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

REQUIRED_ENV_VARS = ("ZENDESK_SUBDOMAIN", "ZENDESK_OAUTH_TOKEN")

# W1-F4. `docs/STATE.md §6.14` names this script's own output — "live_smoke:
# Zendesk credentials absent" — as the visible symptom of the fact that
# nothing in the app or scripts ever called `load_dotenv()`. The credentials
# were sitting in `.env` the whole time; the documented command in this
# module's usage line simply could not see them, so the script reported a
# missing trial that existed and exited 0.
#
# `override=False`, matching `backend/src/main.py`: an operator who exported
# something deliberately still wins. Repo root is derived from this file's
# location, not the working directory, so `uv run python scripts/live_smoke.py`
# behaves the same from anywhere. A missing `.env` is not an error.
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


def _missing_env_vars() -> list[str]:
    return [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]


def main(argv: list[str]) -> int:
    missing = _missing_env_vars()
    if missing:
        print(
            "live_smoke: Zendesk credentials absent "
            f"({', '.join(missing)} not set) — skipping, no network call attempted. "
            "Follow docs/zendesk-runbook.md to fill these into .env, then re-run."
        )
        return 0

    if len(argv) < 2:
        print("usage: uv run python scripts/live_smoke.py <ticket_id>", file=sys.stderr)
        return 2
    ticket_id = argv[1]

    # Imported only after credentials are confirmed present, so a
    # credentials-absent run never constructs an httpx.Client at all.
    from helpdesk.models import EscalationGroup
    from helpdesk.zendesk_adapter import ZendeskAdapter

    adapter = ZendeskAdapter()
    print(f"live_smoke: exercising every HelpdeskPort op against ticket {ticket_id}")

    ticket = adapter.fetch_ticket(ticket_id)
    print(f"  fetch_ticket        -> id={ticket.id} status={ticket.status} tags={ticket.tags}")

    conversation = adapter.fetch_conversation(ticket_id)
    print(f"  fetch_conversation  -> {len(conversation)} message(s)")
    for message in conversation:
        print(f"      [{message.author_kind}] public={message.public} {message.text[:60]!r}")

    note_ref = adapter.post_internal_note(ticket_id, "live_smoke: internal note check.")
    print(f"  post_internal_note  -> message_id={note_ref.message_id} public={note_ref.public}")

    reply_ref = adapter.post_public_reply(
        ticket_id, "<p>live_smoke: public reply check (safe to ignore).</p>"
    )
    print(f"  post_public_reply   -> message_id={reply_ref.message_id} public={reply_ref.public}")

    adapter.add_tags(ticket_id, ["live-smoke-test"])
    print("  add_tags            -> requested ['live-smoke-test']")

    adapter.set_status(ticket_id, "pending")
    print("  set_status          -> requested 'pending'")

    group_id = os.environ.get("ZENDESK_LIVE_SMOKE_GROUP_ID")
    if group_id:
        adapter.assign_group(ticket_id, EscalationGroup(group_id=group_id, name="live-smoke"))
        print(f"  assign_group        -> requested group_id={group_id}")
    else:
        print("  assign_group        -> skipped (ZENDESK_LIVE_SMOKE_GROUP_ID not set)")

    final_ticket = adapter.fetch_ticket(ticket_id)
    if "ai-processed" not in final_ticket.tags:
        print(
            "  FAIL: 'ai-processed' tag missing after live writes — this would "
            "infinite-loop the webhook trigger. tags=" + str(final_ticket.tags),
            file=sys.stderr,
        )
        return 1
    print(f"  ai-processed tag confirmed present: {final_ticket.tags}")

    print("live_smoke: all HelpdeskPort operations completed against the live trial.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
