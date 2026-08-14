#!/usr/bin/env python3
"""claim_lookup.py — shared parser/resolver for the append-only ticket-claim
log at .claude/active-ticket (T-13: session-scoped, append-only ticket
claims).

Used by scope_guard.sh, stop_guard.sh and verify_gate.sh so the claim-log
FORMAT and its interpretation live in exactly one place. Read-only w.r.t.
the claim log itself: this script never writes .claude/active-ticket (the
production writer is .claude/hooks/claim.sh — see that file). It DOES read
the PreToolUse hook payload (via stdin, --mode append-check) to judge
whether a proposed Edit/Write would preserve history — that's a decision,
not a write.

FORMAT (append-only, one record per line, git-tracked, UTF-8, LF-terminated):
  Each non-blank line is either

  (a) a JSON object:
        {"ticket": "<T-id or null>", "session": "<CLAUDE_CODE_SESSION_ID or null>",
         "ts": "<UTC ISO-8601, e.g. 2026-08-14T18:32:07Z, or null>"}
      "ticket": null (with any session/ts) is an explicit RELEASE marker —
      "this session/claim now has no active ticket".

      A record is only trusted as a REAL, exclusively-attributed claim if
      "session" AND "ts" are BOTH non-empty strings (T-13 adversarial
      finding #4). .claude/hooks/claim.sh — the only production writer —
      always supplies both together; a record with a "session" but no "ts"
      cannot have come from it. Such a record is therefore stripped of its
      session (treated exactly like "session": null below) rather than
      trusted at face value: a claim that skipped recording *when* earns
      no more ownership trust than no claim at all.

  (b) a LEGACY bare line: anything that does not parse as a JSON object is
      treated as {"ticket": "<line, whitespace-trimmed>", "session": null,
      "ts": null} — this is exactly the pre-T-13 single-line active-ticket
      format (a bare ticket id, nothing else), preserved so a claim recorded
      before this change is never stranded. See MIGRATION below.

  Blank / whitespace-only lines are ignored. Lines are APPEND-ONLY: this
  script (and every hook that calls it) only ever reads from the end
  backwards or takes the last line — nothing here rewrites or deletes an
  earlier line, and nothing here writes at all. scope_guard.sh's
  --mode append-check (see below) is what gives that property TEETH against
  a raw Edit/Write, rather than leaving it as a convention only the test
  suite's own helpers happen to honour (T-13 adversarial findings #2/#3).

MODES
  --mode last
      The ticket named by the LAST non-blank line, full stop, regardless of
      session. This is what a *path/scope* check needs ("what ticket is
      currently claimed, globally") — deliberately NOT session-aware.
      scope_guard.sh uses only this mode for its per-ticket scope lookup:
      T-13 acceptance 1 names ONLY stop_guard.sh and verify_gate.sh as
      session-scoped, and scope_guard.sh documents in its own header why it
      stays global. This mode is also the DEGRADE target for the other two
      hooks when a session id cannot be determined at all (see their
      headers).

  --mode owned --session S [--strict]
      Scan non-blank lines from the END backwards; return the ticket named
      by the first (i.e. most recent) line whose recorded session equals S.

      A line with NO recorded session (a bare legacy line, an explicit
      {"session": null} record, or a session-without-ts record — see (a)
      above) is UNATTRIBUTED. What happens when the scan reaches one:

        --strict (stop_guard.sh's mode): an unattributed line is NEVER
        treated as S's claim, full stop. T-13 adversarial finding #1: a
        second, genuinely unrelated session in the same working directory
        must NEVER be told to finish or revert a claim it never made — and
        an unattributed line carries no evidence it belongs to any
        particular session, so guessing "yes, that's you" is exactly the
        bug acceptance 2 exists to kill. stop_guard.sh only ever *nags* a
        session to finish/revert/release; verify_gate.sh (below) is the
        hook that actually enforces "done only when verify passes", and it
        does NOT use --strict — so this trade costs a Stop-time reminder
        for one bounded migration window, never the real completion gate.

        non-strict (verify_gate.sh's mode; the default): an unattributed
        line is treated as S's claim ONLY if it is the single most recent
        (last) non-blank line in the ENTIRE log — i.e. nothing, attributed
        or not, has been appended after it. This is the bounded MIGRATION
        case: the live claim in .claude/active-ticket at the moment T-13
        shipped was exactly one bare, unattributed line, and treating that
        one line as "owned by whoever asks" is what keeps verify_gate.sh's
        real completion gate enforced for that in-flight ticket without
        forging an attribution nobody can actually verify (see MIGRATION).
        The INSTANT anything else is appended to the log, this stale line
        stops being handed out to a fresh query (T-13 adversarial finding
        #5) — the ambiguity is over "from then on", exactly as documented
        below, and scanning past it further back returns nothing rather
        than either fabricating an owner or tunnelling through it to an
        older, unrelated line.

      If the line that would otherwise be returned has ticket == null (a
      release marker), this prints nothing — "no open claim" — same as if
      no matching line existed at all.

  --mode append-check
      Not a claim query — a WRITE-SAFETY check. Reads a PreToolUse
      Edit/Write payload as JSON on stdin (the same payload scope_guard.sh
      already has) and prints "ok" if performing that exact tool call
      against `claims_file`'s CURRENT on-disk content would leave that
      content as an exact, byte-identical PREFIX of the result — i.e. the
      call can only ever append new material, never rewrite or delete a
      prior line — or "violate" otherwise (including on any parse error:
      this check fails CLOSED, never open, because an unchecked write here
      is exactly what T-13 adversarial findings #2/#3 identified as the
      hole: nothing previously stopped a plain overwrite from destroying
      the audit trail). See would_preserve_history() below for exactly how
      Write and Edit are each evaluated.

MIGRATION (T-13 constraint 7): the live claim in .claude/active-ticket at
the moment T-13 shipped was the bare legacy line "T-13" — no session, no
timestamp. That file's existing content was deliberately NOT rewritten as
part of landing T-13: the implementing session's own CLAUDE_CODE_SESSION_ID
is not guaranteed to be the true orchestrating session's id (it is a
subagent's view, and subagents are confirmed to share their parent's id —
but nothing forces THIS to be the process that actually holds the live
claim), so forging an attribution into the audit trail risked being simply
wrong. Interpreting a session-less line as ownable-by-whoever-asks, ONLY
while it remains the log's single most recent line, keeps that in-flight
claim enforced for verify_gate.sh's real completion gate without guessing,
while --strict closes the same ambiguity's OTHER failure mode (a false
Stop-block on a genuine bystander) for stop_guard.sh. The next claim
appended by any session (any ticket, including a fresh claim of T-13
itself) should go through .claude/hooks/claim.sh, which ALWAYS records a
real session + timestamp together — unambiguous from then on, for every
mode, with no residual amnesty window at all.

ROBUSTNESS: this script never raises past main() and always exits 0. A
missing file, an unreadable file, or a malformed line resolves to "no
ticket" for --mode last/owned (never a crash), and resolves to "violate"
(fail CLOSED, not open) for --mode append-check — a parsing hiccup here
must not look like a hook execution ERROR to Claude Code, and must never
silently authorise a write whose safety it failed to evaluate.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _parse_line(line: str) -> tuple[str | None, str | None]:
    """Return (ticket, session) for one already-non-blank claim-log line."""
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        obj = None

    if isinstance(obj, dict):
        ticket = obj.get("ticket")
        session = obj.get("session")
        ts = obj.get("ts")
        ticket = ticket if isinstance(ticket, str) and ticket else None
        session = session if isinstance(session, str) and session else None
        ts = ts if isinstance(ts, str) and ts else None
        if session is not None and ts is None:
            # Finding #4: a session claim with no timestamp at all cannot
            # have come from claim.sh (the only production writer, which
            # always writes both together) — treat it as unattributed
            # rather than trusting it at face value.
            session = None
        return ticket, session

    # Legacy: the whole trimmed line is the ticket id; no session recorded.
    legacy_ticket = line.strip()
    return (legacy_ticket or None), None


def _lines(path: Path) -> list[str]:
    try:
        text = path.read_text()
    except OSError:
        return []
    return [ln for ln in text.splitlines() if ln.strip()]


def resolve_last(path: Path) -> str | None:
    lines = _lines(path)
    if not lines:
        return None
    ticket, _session = _parse_line(lines[-1])
    return ticket


def resolve_owned(path: Path, session: str | None, *, strict: bool = False) -> str | None:
    lines = _lines(path)
    n = len(lines)
    for idx in range(n - 1, -1, -1):
        ticket, rec_session = _parse_line(lines[idx])
        if rec_session is not None:
            if rec_session == session:
                return ticket
            # A different, real session's claim — not mine. An older entry
            # further back might still be mine; keep scanning.
            continue
        # rec_session is None: unattributed (bare legacy line, explicit
        # {"session": null}, or a session-without-ts record folded down
        # above).
        if strict:
            return None
        if idx == n - 1:
            # Amnesty, but ONLY for the single most-recent record in the
            # WHOLE log (finding #5). The instant a newer record —
            # attributed or not — sits after it, this stale line is no
            # longer handed out to a fresh query.
            return ticket
        return None
    return None


def would_preserve_history(old: str, tool_name: str, tool_input: dict) -> bool:
    """True iff performing this Edit/Write against CURRENT content `old`
    would leave `old` as an exact prefix of the result — i.e. the call can
    only append, never rewrite or delete prior lines (T-13 acceptance 3;
    adversarial findings #2/#3).
    """
    if tool_name == "Write":
        new = tool_input.get("content")
        new = new if isinstance(new, str) else ""
    else:
        # Edit (or any other tool routed through this path): simulate the
        # str_replace against the CURRENT on-disk content — the same
        # substitution the real Edit tool performs — so the invariant is
        # judged against the actual resulting content.
        old_string = tool_input.get("old_string")
        new_string = tool_input.get("new_string")
        old_string = old_string if isinstance(old_string, str) else ""
        new_string = new_string if isinstance(new_string, str) else ""
        replace_all = bool(tool_input.get("replace_all", False))
        if old_string == "":
            new = old
        elif replace_all:
            new = old.replace(old_string, new_string)
        else:
            new = old.replace(old_string, new_string, 1)
    return new.startswith(old)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("claims_file")
    parser.add_argument("--mode", choices=["last", "owned", "append-check"], required=True)
    parser.add_argument("--session", default=None)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)

    path = Path(args.claims_file)

    if args.mode == "append-check":
        try:
            try:
                old = path.read_text()
            except OSError:
                old = ""
            raw = sys.stdin.read()
            payload = json.loads(raw) if raw.strip() else {}
            if not isinstance(payload, dict):
                payload = {}
            tool_name = payload.get("tool_name")
            tool_name = tool_name if isinstance(tool_name, str) else ""
            tool_input = payload.get("tool_input")
            tool_input = tool_input if isinstance(tool_input, dict) else {}
            ok = would_preserve_history(old, tool_name, tool_input)
        except Exception:
            ok = False  # fail CLOSED — never silently authorise an
            # unevaluated write to the audit trail.
        sys.stdout.write("ok" if ok else "violate")
        return 0

    try:
        if args.mode == "last":
            result = resolve_last(path)
        else:
            result = resolve_owned(path, args.session or None, strict=args.strict)
    except Exception:
        result = None

    if result:
        sys.stdout.write(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
