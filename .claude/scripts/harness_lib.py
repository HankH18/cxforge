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
# NOTE (W15 route 2): `.claude/evidence/` was deliberately REMOVED from this list.
# It was exempt here and gitignored, so a receipt written by a ticket's verify
# command appeared in no diff either integrity() call takes -- one line in a verify
# could mint receipts for arbitrary other tickets and nothing could see it. W23 made
# receipts tracked, which is what lets them be diffed now. The real receipt is written
# AFTER both integrity calls and after the close commit, so honest closes are
# unaffected, and claim-time is unaffected because working_tree_dirty() filters
# evidence via W1_EXEMPT. ABSOLUTE still forbids agent writes there.
HARNESS_STATE = [r"^\.claude/claims/.*$", r"^\.claude/monitor/.*$"]
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
    """Every READABLE claim record, keyed by the session id its filename names.

    Unreadable records are deliberately NOT raised here: one corrupt file used to
    take down every reader that merely wanted to know which tickets are held
    (status/status_board/cmd_claim's collision loop), turning a single broken
    session into a repo-wide outage. They are reported instead by
    incoherent_claims(), and the commands that ACT on a specific record still
    refuse it by name via usable_claim()."""
    out = {}
    if os.path.isdir(CLAIMS):
        for fn in os.listdir(CLAIMS):
            if fn.endswith(".json"):
                try:
                    with open(os.path.join(CLAIMS, fn)) as f: c = json.load(f)
                except Exception: continue
                if isinstance(c, dict): out[fn[:-5]] = c
    return out

def incoherent_claims():
    """{session id: [defects]} for every claim record that cannot be acted on."""
    out = {}
    if os.path.isdir(CLAIMS):
        for fn in sorted(os.listdir(CLAIMS)):
            if not fn.endswith(".json"): continue
            sid = fn[:-5]
            _, defects = usable_claim(sid)
            if defects: out[sid] = defects
    return out

def session_claim(sid):
    p = os.path.join(CLAIMS, f"{sid}.json")
    if os.path.exists(p):
        with open(p) as f: return json.load(f)
    return None

def claim_file(sid):
    """Repo-relative path of a session's claim record. THE FILENAME IS THE ATTRIBUTION:
    .claude/claims/<session>.json is written only by cmd_claim, only for the session whose
    id names it, and every reader (close, release, guard, stop) resolves ownership from
    this path alone. A refusal must print this path — "names the offending record"."""
    return os.path.join(".claude", "claims", f"{sid}.json")

def claim_defects(sid, c):
    """Why this claim record cannot be acted on. Empty list = coherent.

    A claim record is the harness's statement of who owns what. Because the FILENAME is
    the attribution (claim_file), a record whose own "session" field disagrees with it
    asserts two contradictory owners and the harness has nothing to adjudicate with: the
    path says one session, the content says another. That is not a weaker claim, it is an
    INCOHERENT one, and resolving it silently in favour of the filename is exactly what
    minting a receipt used to do — certifying work under an attribution the record itself
    contradicts. Refuse by name instead. Same for a record that carries no "session" at
    all, or no usable ticket/start_commit: unattributed in substance, so nothing may be
    certified for it (T-28 acceptance 1)."""
    if not isinstance(c, dict):
        return ["record is %s, not a JSON object" % type(c).__name__]
    out = []
    for k in ("ticket", "start_commit"):
        v = c.get(k)
        if not (isinstance(v, str) and v.strip()):
            out.append("missing required field %r (absent, empty or not a string)" % k)
    if "session" not in c:
        out.append("no 'session' field at all — the record claims no owner of its own")
    elif not isinstance(c["session"], str) or c["session"] != sid:
        out.append("'session' field %r disagrees with the filename that IS its attribution "
                   "(%r)" % (c["session"], sid))
    return out

def usable_claim(sid):
    """(claim, defects) for one session. Readers must treat a defective record as
    UNUSABLE — neither a valid claim nor an absent one: `defects` non-empty always wins
    over whatever `claim` happens to contain."""
    # "no file at all" is the ONLY absence: a file that exists but parses to null/[]/"" is
    # an incoherent record, not a missing one, and must not be reported as "no claim".
    if not os.path.exists(os.path.join(CLAIMS, f"{sid}.json")):
        return None, []
    try:
        c = session_claim(sid)
    except Exception as e:                      # unparseable / empty / unreadable file
        return None, ["record is unreadable (%s)" % e]
    defects = claim_defects(sid, c)
    return (c if isinstance(c, dict) else None), defects

def incoherent_claim_msg(sid, defects, consequence):
    return ("REFUSING — %s is not a coherent claim record:\n  %s\n"
            "Its filename is the attribution the harness acts on, so a record that "
            "contradicts (or omits) it names no session whose work can be certified: %s. "
            "Only .claude/scripts/claim.sh writes this file — release the claim and record "
            "what happened in .claude/NEEDS_HUMAN.md rather than hand-editing it."
            % (claim_file(sid), "\n  ".join(defects), consequence))

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

class IntegrityUnavailable(Exception):
    """git could not answer "what changed since <commit>" at all.

    Raised rather than returning [] because an empty diff and an unanswerable
    diff are opposite facts: the first says nothing changed, the second says we
    do not know. The old code used `.stdout` regardless of returncode, so a
    start_commit that did not resolve produced an empty list and the integrity
    check passed VACUOUSLY — silently disabling scope enforcement for that close.
    """


# Generated artifacts. Absorbing these into a ticket-start commit launders nothing —
# they carry no plan or source content — and without this the check is defeated by
# ordinary build noise rather than by anything a reviewer would care about.
W1_IGNORED_SEGMENTS = ("__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "node_modules")
W1_IGNORED_SUFFIXES = (".pyc", ".pyo", ".DS_Store")
# Inert legacy closure records: same class as HARNESS_STATE, never source, never plan.
# `.claude/evidence/` joins the v1 entry here for the W23 reason: receipts became
# tracked so the attestation chain survives a clone, and a receipt is minted
# AFTER the close commit (it records that commit's hash), so it is left
# uncommitted and would otherwise leave the tree dirty and refuse the NEXT
# claim. The next ticket-start commit absorbs it, exactly as it always has for
# .claude/claims/. No hole: W1 exists to stop an agent laundering its own
# pre-existing work into a start commit, and receipts are not agent-writable at
# all (.claude/evidence/** is in ABSOLUTE); they were already exempt from the
# close-time integrity check via HARNESS_STATE.
W1_EXEMPT = [r"^\.claude/evidence-v1/.*$", r"^\.claude/evidence/.*$"]


def _w1_noise(path):
    if any(seg in path.split("/") for seg in W1_IGNORED_SEGMENTS):
        return True
    return path.endswith(W1_IGNORED_SUFFIXES)


def working_tree_dirty():
    """Paths with uncommitted changes right now (staged, unstaged, untracked).

    `-uall` matters: without it git reports an untracked DIRECTORY as one entry with a
    trailing slash, which no scope glob can match — so a new directory full of
    out-of-scope source would slip past while build noise blocked honest claims. Listing
    files individually makes the check both stricter on real content and quieter on junk.

    `--no-renames` and `-z` matter for the same reason, one level down. In porcelain v1's
    default text format a rename is ONE record naming two paths, `R  <old> -> <new>`, and
    a reader that keeps only the destination stops seeing the source at all: `git mv
    docs/SPEC.md src/SPEC.md` reported just `src/SPEC.md`, which matches a `src/**` scope,
    so the gate passed and the ticket-start commit absorbed the DELETION of a protected
    plan file. Splitting on the literal `" -> "` was wrong twice over — it also fired on
    plain `M` records whose path merely CONTAINS that substring, rewriting an out-of-scope
    path into its own in-scope-looking suffix. `--no-renames` makes git emit the two paths
    as two independent records (D + A) so there is no pair to mis-read, and `-z` removes
    the C-quoting that the old `.strip('"')` was papering over. No string surgery is left.

    A `git status` that FAILS is not a clean tree — that is the same "empty answer vs. no
    answer" conflation IntegrityUnavailable exists to prevent, so it raises rather than
    returning [] and letting the claim proceed on a vacuous pass."""
    r = subprocess.run(["git", "status", "--porcelain", "-z", "-uall", "--no-renames"],
                       capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0:
        raise IntegrityUnavailable(
            "git could not report the working tree state (%s). Whether anything is "
            "already dirty is therefore unknown, so the claim is refused rather than "
            "granted on an unanswered question." % (r.stderr or "").strip()[:200]
        )
    out = []
    for rec in r.stdout.split("\0"):
        # -z records are `XY <path>` with NO quoting and NO rename pairing.
        if len(rec) < 4:
            continue
        p = rec[3:]
        if _w1_noise(p) or match_any(p, W1_EXEMPT, raw=True):
            continue
        out.append(p)
    return sorted(set(out))


def changed_since(commit):
    # --no-renames for the same reason working_tree_dirty() passes it: with rename
    # detection on, `git diff --name-only` prints ONLY the destination, so a rename that
    # moved a protected plan file out of the way during a ticket was invisible to the
    # close-time check and the receipt was minted clean. Off, both paths are listed.
    subprocess.run(["git", "add", "-A"], cwd=ROOT)
    # -z for the same reason working_tree_dirty() passes it: without it git
    # C-QUOTES any path containing non-ASCII bytes -- `"src/caf\303\251.txt"`,
    # quotes and octal escapes included. That form matches no scope glob, so a
    # legitimately in-scope file was reported OUT of scope and the close refused.
    # The refusal then left the verify's output staged, blocking every later claim
    # via the W1 dirty check, without incrementing `attempts` so the 2-strikes
    # release never tripped. With -z the paths arrive raw, NUL-separated.
    r = subprocess.run(["git", "diff", "--name-only", "-z", "--no-renames", commit], capture_output=True, text=True, cwd=ROOT)
    s = subprocess.run(["git", "diff", "--name-only", "-z", "--no-renames", "--cached", commit], capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0 or s.returncode != 0:
        raise IntegrityUnavailable(
            "git could not diff against start_commit %r (%s). The integrity check "
            "cannot be evaluated, so this close is refused rather than passed."
            % (commit, (r.stderr or s.stderr).strip()[:200])
        )
    return sorted({p for p in r.stdout.split("\0") if p} | {p for p in s.stdout.split("\0") if p})

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
        held, defects = usable_claim(sid)
        if defects:
            # A1/C1: this used to be `session_claim(sid)['ticket']`, which crashed with a
            # raw traceback on exactly the records the harness most needs to talk about.
            return incoherent_claim_msg(
                sid, defects, "it can neither be closed nor re-claimed — release it "
                "with a reason first"), 1
        return f"this session already owns {held['ticket']} — close or release first", 1
    for s, c in claims().items():
        if c.get("ticket") == tid: return f"{tid} is claimed by another session ({s}) — do not touch it", 1
    t = ticket(tid)
    if not t: return f"no ticket {tid} in docs/tickets.json", 1
    if receipt(tid): return f"{tid} already has a receipt (resolved)", 1
    for d in t.get("depends_on", []):
        if not receipt(d): return f"blocked: dependency {d} has no receipt", 1
    errs = lint_verify(t["verify"])
    if errs: return "verify-string lint (fix the plan first):\n  " + "\n  ".join(errs), 1
    # W1: the ticket-start commit sweeps the whole tree (`git add -A`), and
    # integrity() only diffs FORWARD from it — so anything already dirty here
    # would be absorbed into the start commit and never checked at close. That
    # made release+re-claim a general laundering route for any file, PROTECTED
    # ones included. Refuse instead: out-of-scope work gets committed in the
    # open, as its own commit, before a ticket claims the tree.
    try:
        tree = working_tree_dirty()
    except IntegrityUnavailable as e:
        return str(e), 1
    dirty = [p for p in tree
             if not (match_any(p, t["scope"]) or match_any(p, META_ALLOW, raw=True)
                     or match_any(p, HARNESS_STATE, raw=True))]
    if dirty:
        return ("REFUSING to claim %s — the working tree carries changes outside its scope:\n  %s\n"
                "The ticket-start commit would absorb these and the close-time integrity check "
                "would never see them. Commit them as their own commit (or revert them) first, "
                "then claim." % (tid, "\n  ".join(dirty))), 1
    _git("add", "-A"); _git("commit", "-q", "--allow-empty", "-m", f"ticket-start: {tid}")
    start = _head()
    if not start:
        # Without a resolvable HEAD the record would carry start_commit "" and the close
        # could never evaluate integrity against it — a claim that can only ever be
        # released. Refuse now, while nothing has been written.
        return ("refusing to claim %s: git could not resolve HEAD after the ticket-start "
                "commit, so the claim record would carry no start commit and its close "
                "could never be integrity-checked." % tid), 1
    _write(cpath, {"ticket": tid, "session": sid, "note": note,
                   "start_commit": start, "attempts": 0, "ts": int(time.time())})
    return f"claimed {tid} @ {_head(True)} | scope: {t['scope']}", 0

def cmd_close():
    sid = _sid()
    # C1/A1: a claim record that is unreadable, parses to a non-object, is missing
    # required fields, or whose own "session" field disagrees with the filename that IS
    # its attribution is refused BY NAME here — before any gate runs and before any
    # evidence exists. The mismatch case used to be the worst of the set: no crash, no
    # warning, a normal receipt minted for a record the harness could not coherently
    # attribute.
    c, defects = usable_claim(sid)
    if defects:
        return incoherent_claim_msg(
            sid, defects, "no gate is run and no evidence is written for it"), 1
    if not c: return "no claim held by this session", 1
    tid, start = c["ticket"], c["start_commit"]; t = ticket(tid)
    if not t: return f"claim names {tid}, which is not in docs/tickets.json", 1
    if not (t.get("verify") or "").strip():
        # C3: an empty verify trivially "passes" (`sh -c ""` exits 0).
        return (f"{tid} has no verify command — closing would mint a fingerprint-bound receipt "
                f"certifying that nothing was checked. Fix the plan."), 2
    if _git("rev-parse", "--verify", "--quiet", start + "^{commit}").returncode != 0:
        # C2: this used to leave integrity() passing vacuously. Names the record too, so
        # every close-time refusal points at the same file the human has to look at.
        return (f"{claim_file(sid)}: claim's start_commit {start!r} does not resolve to a commit "
                f"in this repo, so the close-time integrity check cannot be evaluated. Refusing "
                f"rather than minting a receipt on an unchecked diff."), 2
    try:
        bad = integrity(tid, start)
    except IntegrityUnavailable as e:
        return str(e), 2
    if bad:
        return ("INTEGRITY FAIL — files changed outside %s scope/meta:\n  %s\n"
                "Out-of-scope changes (including via Bash) fail the close. Revert them or record the plan defect in .claude/NEEDS_HUMAN.md." % (tid, "\n  ".join(bad))), 2
    def fail(extra=""):
        # .get: a record with no "attempts" is malformed but not MISattributed, so it
        # reaches here; counting from 0 beats a KeyError traceback mid-close.
        c["attempts"] = int(c.get("attempts") or 0) + 1
        _write(os.path.join(CLAIMS, f"{sid}.json"), c)
        msg = (f"VERIFY FAIL for {tid} (attempt {c['attempts']}).{extra} Materiality rule: style/lint-only "
               f"failures may be fixed in place; behavioral failures require 'git reset --hard {start}' first.")
        if c["attempts"] >= 2:
            msg += "\n2 failed attempts: release with a reason, add details to .claude/NEEDS_HUMAN.md, and stop for the human."
        return msg, 2
    if subprocess.run(t["verify"], shell=True, cwd=ROOT).returncode != 0: return fail()
    full = load_tickets().get("full_verify")
    if full and subprocess.run(full, shell=True, cwd=ROOT).returncode != 0:
        return fail(" (cross-ticket regression gate)")
    # W15: the two commands above are arbitrary shell, and integrity() ran BEFORE
    # them. Anything they wrote — a regenerated report, a rewritten fixture, an
    # edited gate artifact — would otherwise reach the `git add -A` below having
    # never been scope-checked, and be attested by the receipt. Re-check against
    # the same start commit so a verify cannot be a side door into an attested
    # commit. Both calls must agree; passing the first and failing this one means
    # the verify itself wrote out of scope.
    try:
        bad_after = integrity(tid, start)
    except IntegrityUnavailable as e:
        # Same rule as the pre-verify call: unevaluable is not the same as clean.
        return str(e), 2
    if bad_after:
        return ("INTEGRITY FAIL (post-verify) — %s's verify command wrote outside its scope:\n  %s\n"
                "These files were written by the verify itself, after the pre-verify check passed, so "
                "no scope check had been applied to them. A verify may not be a write channel into the "
                "commit it certifies. Fix the verify command or the ticket's scope, or record the plan "
                "defect in .claude/NEEDS_HUMAN.md." % (tid, "\n  ".join(bad_after))), 2
    _git("add", "-A"); _git("commit", "-q", "--allow-empty", "-m", f"ticket-close: {tid}")
    fp = fingerprint(t["scope"])
    _write(os.path.join(EVID, f"{tid}.json"),
           {"ticket": tid, "session": sid, "verify": t["verify"], "commit": _head(),
            "fingerprint": fp, "attempts": int(c.get("attempts") or 0), "ts": int(time.time())})
    os.remove(os.path.join(CLAIMS, f"{sid}.json"))
    # NOTE (W23): the receipt is deliberately NOT committed here. It records the
    # close commit's own hash, so committing it would move HEAD past the commit
    # it certifies and break the receipt-binds-to-HEAD invariant T-29 enforces
    # (three tests assert exactly that). It is tracked, and the next
    # `ticket-start` commit absorbs it -- the same lifecycle .claude/claims/ has
    # always had. `.claude/evidence/` is exempt from the claim-time dirty check
    # so that pending receipt cannot block the next claim.
    try:
        subprocess.run([sys.executable, os.path.join(ROOT, ".claude/scripts/gen_tasks.py")],
                       capture_output=True, cwd=ROOT)
    except Exception: pass
    return f"closed {tid} | receipt bound to {_head(True)} fp={fp[:12]}…", 0

def cmd_release(reason):
    sid = _sid(); c, defects = usable_claim(sid)
    if not c and not defects:
        return "no claim held by this session", 1
    # An incoherent record must still be RELEASABLE, or refusing it at close would wedge
    # the session: it may not hand-edit .claude/claims/** (ABSOLUTE) and close now refuses
    # it, so release is the only sanctioned exit. Release certifies nothing — it writes no
    # evidence — so degrading here cannot launder anything; it just retires the record and
    # tells the human, which is precisely where a corrupt record belongs.
    tid = (c or {}).get("ticket")
    tid = tid if isinstance(tid, str) and tid.strip() else "<unattributable ticket>"
    detail = ("" if not defects else
              " [INCOHERENT CLAIM RECORD %s: %s]" % (claim_file(sid), "; ".join(defects)))
    with open(os.path.join(ROOT, ".claude", "NEEDS_HUMAN.md"), "a") as f:
        f.write(f"- [{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {tid} released by {sid}: {reason}{detail}\n")
    os.remove(os.path.join(CLAIMS, f"{sid}.json"))
    return f"released {tid} (logged to NEEDS_HUMAN.md){detail}", 0

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
        ssid = p.get("session_id") or ""
        c, defects = usable_claim(ssid)
        if defects:
            # An incoherent record is still an OPEN one, and this used to be a raw
            # traceback (unparseable JSON) or a silent pass (misattributed). Letting the
            # session stop on it is how a broken claim outlives the session that made it.
            return json.dumps({"decision": "block", "reason": incoherent_claim_msg(
                ssid, defects,
                "it can neither be closed nor certified — release it with a reason before "
                "stopping, and put the details in .claude/NEEDS_HUMAN.md")}), 0
        if not c: return "", 0
        reason = (f"This session holds an open claim on {c['ticket']} (attempts: {c.get('attempts', 0)}). Before stopping: "
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
        # Surfaced, never fatal: a board that crashes on one broken record tells nobody
        # anything, and a board that hides it lets an unattributable claim sit unnoticed.
        for bad_sid, defects in incoherent_claims().items():
            print("  !!  INCOHERENT  %s: %s — release it and record it in "
                  ".claude/NEEDS_HUMAN.md" % (claim_file(bad_sid), "; ".join(defects)))
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
            # An incoherent claim record unlocks nothing and constrains nothing coherently
            # (its scope would come from a ticket it cannot be trusted to name), so it
            # fails CLOSED for every in-repo write. META_ALLOW and ABSOLUTE above are
            # deliberately checked first: NEEDS_HUMAN.md stays writable so the session can
            # report this, and attestation state stays denied for its own stronger reason.
            c0, defects0 = usable_claim(sid) if os.path.exists(TICKETS) else (None, [])
            if defects0:
                print("deny:" + incoherent_claim_msg(
                    sid, defects0, "no in-repo write can be judged against it").replace("\n", " "))
                sys.exit(0)
            if match_any(rel, PROTECTED, raw=True):
                t0 = ticket(c0["ticket"]) if c0 else None
                if not (t0 and match_any(rel, t0["scope"])):
                    print("deny:%s is a plan/harness file. These change only through sanctioned scripts, human-approved plan fixes, or a ticket whose own scope names them — if this ticket needs it changed, that's a plan defect: record it in .claude/NEEDS_HUMAN.md and stop." % rel); sys.exit(0)
                print("allow"); sys.exit(0)           # sanctioned by the claimed ticket's contract
            if not os.path.exists(TICKETS):
                print("allow"); sys.exit(0)           # no plan installed yet; harness inert except protected files
            c = c0
            if not c: print("allow"); sys.exit(0)     # non-owning sessions are unconstrained (jarvis T-21)
            t = ticket(c["ticket"])
            if t and match_any(rel, t["scope"]): print("allow")
            else: print("deny:%s is outside %s scope %s. If genuinely required, that's a plan defect — NEEDS_HUMAN.md, then stop." % (rel, c["ticket"], t["scope"] if t else "?"))
        except Exception as e:
            print("deny:scope guard internal error (%s) — failing closed for in-repo writes" % e)  # othram B3
    elif fn == "fingerprint":
        print(fingerprint(ticket(sys.argv[2])["scope"]))
    elif fn == "integrity":
        try:
            bad = integrity(sys.argv[2], sys.argv[3])
        except IntegrityUnavailable as e:
            # exit 2, not 1: "these files are out of scope" (1) and "the question could
            # not be asked at all" (2) are different answers and must not share a code,
            # which is the whole reason IntegrityUnavailable exists. A raw traceback here
            # also printed nothing on stdout, so a caller reading stdout saw a clean pass.
            print(str(e)); sys.exit(2)
        if bad: print("\n".join(bad)); sys.exit(1)
    elif fn == "lint":
        t = ticket(sys.argv[2]); errs = lint_verify(t["verify"])
        if errs: print("\n".join(f"verify-string lint: {e}" for e in errs)); sys.exit(1)

