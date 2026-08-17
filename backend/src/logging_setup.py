"""Structured logging for the two long-running processes (W2-C2).

Before this module the entire application had **two** ``logger.warning``
calls and no logging configuration anywhere. Nothing called
``basicConfig``, so Python's last-resort handler applied: WARNING and above
went to stderr as a bare message with no timestamp, no logger name and no
context, and every ``logger.info`` in the tree was discarded. The worker's
ERROR on a failed run — the *only* honest signal that a run failed, because
arq books a swallowed exception as a completed job (`worker.main.run_ticket`
says so at length) — was a naked line of prose with no ticket id attached to
anything machine-readable.

Two decisions worth stating, because both are the kind that get quietly
reverted:

**JSON lines, not a pretty format.** These processes run under
`docker compose`; their output is read with ``docker compose logs`` and,
during the demo, grepped for one ticket. One object per line means
``... | jq 'select(.ticket_id=="123")'`` works, and it survives the
interleaving of two containers' stdout. Human readability is what
``jq`` is for.

**No new environment variable.** A ``LOG_LEVEL`` read would be a fine idea
and is deliberately not here: `backend/tests/deploy/test_env_forwarding.py`
requires every variable named in ``backend/src/**`` to be declared in
`.env.example` and forwarded by both compose files, and those three files
belong to another track's row of the ownership matrix
(`docs/BUILD-PLAN.md §8`). ``level`` is a parameter instead; the caller
decides. See W2-C's report for the exact lines to add if the knob is
wanted.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import IO, Any

# Every attribute the logging module itself puts on a record. Computed from
# a real record rather than hand-listed, because the list changes between
# Python versions (3.12 added ``taskName``) and a hand-list that fell behind
# would start leaking framework internals into the structured output as if
# they were the caller's context.
_STANDARD_RECORD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)) | {"message": "", "asctime": ""}
)


class JsonLogFormatter(logging.Formatter):
    """One JSON object per record.

    Anything passed as ``extra={...}`` becomes a top-level key, so a call
    site adds context by naming it rather than by interpolating it into
    prose that only a human can parse back out.
    """

    def __init__(self, *, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "service": self.service,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_RECORD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        # ``default=str`` rather than a failure: a log line is the thing you
        # have left when everything else has gone wrong, and it must not be
        # the thing that raises.
        return json.dumps(payload, default=str)


def configure_logging(
    *, service: str, level: int = logging.INFO, stream: IO[str] | None = None
) -> logging.Handler:
    """Install one JSON handler on the root logger. Returns it.

    Replaces the root's handlers rather than appending, so calling this
    twice in a process (import-time plus a startup hook, say) cannot produce
    every line twice. stdout, not stderr: under `docker compose` both are
    captured, and stdout is what a log shipper defaults to reading.
    """
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonLogFormatter(service=service))
    root = logging.getLogger()
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    return handler
