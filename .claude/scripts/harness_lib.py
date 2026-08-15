#!/usr/bin/env python3
"""Shared harness logic: glob matching, status derivation, fingerprints, integrity.
Status is DERIVED, never stored (kills the bookkeeping treadmill):
  queue = no claim & no receipt | in_progress = claim exists | resolved = receipt exists
"""
import hashlib, json, os, re, subprocess, sys, time

ROOT = os.environ.get("CLAUDE_PROJECT_DIR") or subprocess.run(
    ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True).stdout.strip()
TICKETS = os.path.join(ROOT, "docs", "tickets.json")
CLAIMS = os.path.join(ROOT, ".claude", "claims")
EVID = os.path.join(ROOT, ".claude", "evidence")

# Paths every session may always write (agent<->human channel, monitor workspace)
META_ALLOW = [r"^\.claude/NEEDS_HUMAN\.md$", r"^\.claude/monitor/.*$", r"^docs/TASKS\.md$"]
# Harness-written state: exempt from the close-time integrity diff (it changes at every
# boundary by design) but still Edit/Write-DENIED via PROTECTED — only scripts write it.
HARNESS_STATE = [r"^\.claude/claims/.*$", r"^\.claude/evidence/.*$", r"^\.claude/monitor/.*$"]
# Receipts and claims: NEVER agent-writable, by any session, under any claim. These are
# the harness's own attestations — an agent that can forge a receipt can certify anything,
# so no ticket scope may unlock them. Checked before PROTECTED; never yields to scope.
ABSOLUTE = [r"^\.claude/claims/.*$", r"^\.claude/evidence/.*$"]
# Plan + harness files: denied by default — a session not given these in a ticket contract
# has no business editing the plan or the harness. Unlike ABSOLUTE this YIELDS to an
# explicit scope grant: if the CLAIMED ticket's own scope names the path, the plan has
# sanctioned the edit and the close-time integrity check will attest it. Without that
# yield, T-22/T-26/T-27/T-28/T-29 are unimplementable — their contracts name exactly the
# paths the guard refuses (D1 in .claude/NEEDS_HUMAN.md).
PROTECTED = [r"^docs/tickets\.json$", r"^docs/SPEC\.md$", r"^docs/DESIGN\.md$",
             r"^\.claude/hooks/.*$", r"^\.claude/scripts/.*$", r"^\.claude/settings\.json$",
             r"^\.claude/claims/.*$", r"^\.claude/evidence/.*$"]

def glob_re(g):
    # Anchored BOTH ends (jarvisforge fix: unanchored search let backend/... slide past scope)
    out, i = "", 0
    while i < len(g):
        if g.startswith("**", i): out += ".*"; i += 2
        elif g[i] == "*": out += "[^/]*"; i += 1
        elif g[i] == "?": out += "[^/]"; i += 1
        else: out += re.escape(g[i]); i += 1
    return re.compile("^" + out + "$")

def match_any(path, patterns, raw=False):
    pats = [re.compile(p) for p in patterns] if raw else [glob_re(p) for p in patterns]
    return any(p.match(path) for p in pats)

def load_tickets():
    with open(TICKETS) as f: return json.load(f)

def ticket(tid):
    for t in load_tickets()["tickets"]:
        if t["id"] == tid: return t
    return None

def claims():
    out = {}
    if os.path.isdir(CLAIMS):
        for fn in os.listdir(CLAIMS):
            if fn.endswith(".json"):
                with open(os.path.join(CLAIMS, fn)) as f:
                    c = json.load(f); out[fn[:-5]] = c
    return out

def session_claim(sid):
    p = os.path.join(CLAIMS, f"{sid}.json")
    if os.path.exists(p):
        with open(p) as f: return json.load(f)
    return None

def receipt(tid):
    p = os.path.join(EVID, f"{tid}.json")
    if os.path.exists(p):
        with open(p) as f: return json.load(f)
    return None

def status(tid):
    if receipt(tid): return "resolved"
    for c in claims().values():
        if c.get("ticket") == tid: return "in_progress"
    return "queue"

def scope_files(scope):
    files = set()
    for g in scope:
        r = subprocess.run(["git", "ls-files", "--", g], capture_output=True, text=True, cwd=ROOT)
        files.update(x for x in r.stdout.splitlines() if x)
    return sorted(files)

def fingerprint(scope):
    # Content fingerprint over the scope's actual bytes (jarvisforge T-22 design):
    # receipts attest WHAT was verified, not just when.
    h = hashlib.sha256()
    for f in scope_files(scope):
        h.update(f.encode())
        try:
            with open(os.path.join(ROOT, f), "rb") as fh: h.update(hashlib.sha256(fh.read()).digest())
        except OSError: h.update(b"<unreadable>")
    return h.hexdigest()

def changed_since(commit):
    subprocess.run(["git", "add", "-A"], cwd=ROOT)
    r = subprocess.run(["git", "diff", "--name-only", commit], capture_output=True, text=True, cwd=ROOT)
    s = subprocess.run(["git", "diff", "--name-only", "--cached", commit], capture_output=True, text=True, cwd=ROOT)
    return sorted(set(r.stdout.splitlines()) | set(s.stdout.splitlines()))

def integrity(tid, start_commit):
    """Every file changed during the ticket must be in scope or meta-allowed.
    This is the compensating control for the documented Bash hole: out-of-band
    edits to protected files (the plan, the hooks) fail the close."""
    t = ticket(tid)
    bad = [p for p in changed_since(start_commit)
           if p and not (match_any(p, t["scope"]) or match_any(p, META_ALLOW, raw=True)
                         or match_any(p, HARNESS_STATE, raw=True))]
    return bad

LINT_RULES = [
    (r"\|\|\s*true", "'|| true' makes the gate pass unconditionally"),
    (r"(?<![($])\bcd\s+\S+\s*&&", "bare 'cd' across '&&' persists directory state; use a subshell '(cd X && ...)'"),
    (r"^\s*echo\b(?!.*&&)", "echo-only verify is self-test evidence, not grounding evidence"),
]
def lint_verify(cmd):
    return [msg for rx, msg in LINT_RULES if re.search(rx, cmd)]

# ---------------- lifecycle (moved here so the harness needs only python3+git, no jq) ------------
def _git(*a): return subprocess.run(["git", *a], capture_output=True, text=True, cwd=ROOT)
def _head(short=False): return _git("rev-parse", *( ["--short"] if short else [] ), "HEAD").stdout.strip()
def _sid(): return os.environ.get("CLAUDE_CODE_SESSION_ID") or f"manual-{os.getppid()}"
def _write(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f: json.dump(obj, f, indent=1)

def cmd_claim(tid, note):
    if not os.path.exists(TICKETS):
        return "no docs/tickets.json in this repo — generate the build docs first (build-docs-cc skill), then claim", 1
    sid = _sid(); cpath = os.path.join(CLAIMS, f"{sid}.json")
    if os.path.exists(cpath):
        return f"this session already owns {session_claim(sid)['ticket']} — close or release first", 1
    for s, c in claims().items():
        if c.get("ticket") == tid: return f"{tid} is claimed by another session ({s}) — do not touch it", 1
    t = ticket(tid)
    if not t: return f"no ticket {tid} in docs/tickets.json", 1
    if receipt(tid): return f"{tid} already has a receipt (resolved)", 1
    for d in t.get("depends_on", []):
        if not receipt(d): return f"blocked: dependency {d} has no receipt", 1
    errs = lint_verify(t["verify"])
    if errs: return "verify-string lint (fix the plan first):\n  " + "\n  ".join(errs), 1
    _git("add", "-A"); _git("commit", "-q", "--allow-empty", "-m", f"ticket-start: {tid}")
    _write(cpath, {"ticket": tid, "session": sid, "note": note,
                   "start_commit": _head(), "attempts": 0, "ts": int(time.time())})
    return f"claimed {tid} @ {_head(True)} | scope: {t['scope']}", 0

def cmd_close():
    sid = _sid(); c = session_claim(sid)
    if not c: return "no claim held by this session", 1
    tid, start = c["ticket"], c["start_commit"]; t = ticket(tid)
    bad = integrity(tid, start)
    if bad:
        return ("INTEGRITY FAIL — files changed outside %s scope/meta:\n  %s\n"
                "Out-of-scope changes (including via Bash) fail the close. Revert them or record the plan defect in .claude/NEEDS_HUMAN.md." % (tid, "\n  ".join(bad))), 2
    def fail(extra=""):
        c["attempts"] += 1; _write(os.path.join(CLAIMS, f"{sid}.json"), c)
        msg = (f"VERIFY FAIL for {tid} (attempt {c['attempts']}).{extra} Materiality rule: style/lint-only "
               f"failures may be fixed in place; behavioral failures require 'git reset --hard {start}' first.")
        if c["attempts"] >= 2:
            msg += "\n2 failed attempts: release with a reason, add details to .claude/NEEDS_HUMAN.md, and stop for the human."
        return msg, 2
    if subprocess.run(t["verify"], shell=True, cwd=ROOT).returncode != 0: return fail()
    full = load_tickets().get("full_verify")
    if full and subprocess.run(full, shell=True, cwd=ROOT).returncode != 0:
        return fail(" (cross-ticket regression gate)")
    _git("add", "-A"); _git("commit", "-q", "--allow-empty", "-m", f"ticket-close: {tid}")
    fp = fingerprint(t["scope"])
    _write(os.path.join(EVID, f"{tid}.json"),
           {"ticket": tid, "session": sid, "verify": t["verify"], "commit": _head(),
            "fingerprint": fp, "attempts": c["attempts"], "ts": int(time.time())})
    os.remove(os.path.join(CLAIMS, f"{sid}.json"))
    try:
        subprocess.run([sys.executable, os.path.join(ROOT, ".claude/scripts/gen_tasks.py")],
                       capture_output=True, cwd=ROOT)
    except Exception: pass
    return f"closed {tid} | receipt bound to {_head(True)} fp={fp[:12]}…", 0

def cmd_release(reason):
    sid = _sid(); c = session_claim(sid)
    if not c: return "no claim held by this session", 1
    with open(os.path.join(ROOT, ".claude", "NEEDS_HUMAN.md"), "a") as f:
        f.write(f"- [{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {c['ticket']} released by {sid}: {reason}\n")
    os.remove(os.path.join(CLAIMS, f"{sid}.json"))
    return f"released {c['ticket']} (logged to NEEDS_HUMAN.md)", 0

def hook_stdin():
    try: return json.load(sys.stdin)
    except Exception: return {}

def cmd_hook(kind):
    p = hook_stdin()
    if kind == "scope":
        fpath = (p.get("tool_input") or {}).get("file_path")
        if not fpath: return "", 0
        res = subprocess.run([sys.executable, os.path.abspath(__file__), "guard",
                              p.get("session_id") or "", fpath], capture_output=True, text=True)
        out = (res.stdout or "").strip().splitlines()
        verdict = out[-1] if out else "deny:guard produced no output; failing closed"
        if verdict == "allow": return "", 0
        reason = verdict[5:] if verdict.startswith("deny:") else verdict
        return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                "permissionDecision": "deny", "permissionDecisionReason": reason}}), 0
    if kind == "stop":
        if p.get("stop_hook_active"): return "", 0
        c = session_claim(p.get("session_id") or "")
        if not c: return "", 0
        reason = (f"This session holds an open claim on {c['ticket']} (attempts: {c['attempts']}). Before stopping: "
                  f"(a) finish it — python3 .claude/scripts/harness_lib.py close; (b) on a behavioral failure, "
                  f"git reset --hard {c['start_commit']} and retry; or (c) release it with a reason and put any "
                  f"FOR-HANK items in .claude/NEEDS_HUMAN.md, not commit bodies. This applies only to YOUR claim; "
                  f"never touch another session's.")
        return json.dumps({"decision": "block", "reason": reason}), 0
    if kind == "taskgate":
        ti = p.get("tool_input") or {}; task = p.get("task") or {}
        subj = ti.get("subject") or task.get("subject") or p.get("subject") or ""
        newst = ti.get("status") or "completed"
        m = re.match(r"^(T-\d+)", subj)
        if not m or newst != "completed": return "", 0
        if receipt(m.group(1)): return "", 0
        sys.stderr.write(f"no receipt for {m.group(1)}: close it via harness_lib.py close before marking the task completed\n")
        return "", 2
    if kind == "heartbeat":
        d = os.path.join(ROOT, ".claude", "monitor"); os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "heartbeat.jsonl"), "a") as f:
            f.write(json.dumps({"ts": int(time.time()), "session": p.get("session_id", "?"),
                                "tool": p.get("tool_name", "?")}) + "\n")
        return "", 0
    return "", 0

def main2():
    fn = sys.argv[1]
    if fn == "claim": out, rc = cmd_claim(sys.argv[2], sys.argv[3])
    elif fn == "close": out, rc = cmd_close()
    elif fn == "release": out, rc = cmd_release(sys.argv[2])
    elif fn.startswith("hook-"): out, rc = cmd_hook(fn[5:])
    else: return None
    if out: print(out)
    sys.exit(rc)

if __name__ == "__main__":
    if sys.argv[1] in ("claim","close","release") or sys.argv[1].startswith("hook-"):
        main2()
    fn = sys.argv[1]
    if fn == "status_board":
        if not os.path.exists(TICKETS):
            print("no docs/tickets.json — harness installed, plan not yet generated"); sys.exit(0)
        for t in load_tickets()["tickets"]:
            print(f"{t['id']:>7}  {status(t['id']):<12} {t['title']}")
    elif fn == "guard":
        # guard <sid> <abspath> -> allow | deny:<reason>   (fail-closed: errors deny in-repo)
        sid, path = sys.argv[2], sys.argv[3]
        try:
            rp = os.path.realpath(path)
            root = os.path.realpath(ROOT)
            if not rp.startswith(root + os.sep):
                print("allow"); sys.exit(0)          # out-of-repo scratchpads are allowed (jarvis/othram A1)
            rel = os.path.relpath(rp, root)
            if match_any(rel, META_ALLOW, raw=True): print("allow"); sys.exit(0)
            if match_any(rel, ABSOLUTE, raw=True):
                print("deny:%s is harness-written attestation state (claims and receipts). No ticket scope unlocks it — only the lifecycle scripts write here. If a receipt or claim looks wrong, say so in .claude/NEEDS_HUMAN.md." % rel); sys.exit(0)
            if match_any(rel, PROTECTED, raw=True):
                c0 = session_claim(sid) if os.path.exists(TICKETS) else None
                t0 = ticket(c0["ticket"]) if c0 else None
                if not (t0 and match_any(rel, t0["scope"])):
                    print("deny:%s is a plan/harness file. These change only through sanctioned scripts, human-approved plan fixes, or a ticket whose own scope names them — if this ticket needs it changed, that's a plan defect: record it in .claude/NEEDS_HUMAN.md and stop." % rel); sys.exit(0)
                print("allow"); sys.exit(0)           # sanctioned by the claimed ticket's contract
            if not os.path.exists(TICKETS):
                print("allow"); sys.exit(0)           # no plan installed yet; harness inert except protected files
            c = session_claim(sid)
            if not c: print("allow"); sys.exit(0)     # non-owning sessions are unconstrained (jarvis T-21)
            t = ticket(c["ticket"])
            if t and match_any(rel, t["scope"]): print("allow")
            else: print("deny:%s is outside %s scope %s. If genuinely required, that's a plan defect — NEEDS_HUMAN.md, then stop." % (rel, c["ticket"], t["scope"] if t else "?"))
        except Exception as e:
            print("deny:scope guard internal error (%s) — failing closed for in-repo writes" % e)  # othram B3
    elif fn == "fingerprint":
        print(fingerprint(ticket(sys.argv[2])["scope"]))
    elif fn == "integrity":
        bad = integrity(sys.argv[2], sys.argv[3])
        if bad: print("\n".join(bad)); sys.exit(1)
    elif fn == "lint":
        t = ticket(sys.argv[2]); errs = lint_verify(t["verify"])
        if errs: print("\n".join(f"verify-string lint: {e}" for e in errs)); sys.exit(1)

