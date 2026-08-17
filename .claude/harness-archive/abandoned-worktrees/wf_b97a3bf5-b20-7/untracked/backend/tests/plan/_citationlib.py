"""Citation- and attribution-integrity checks over the real repo.

Not a test module (pytest only collects ``test_*.py``); imported by
test_citation_integrity.py, and runnable directly as a review tool:

    uv run python backend/tests/plan/_citationlib.py

Three independent detectors, one per observed defect class. All three read
the ACTUAL working tree and the ACTUAL git history -- none of them is
pointed at a fixture. (test_citation_integrity.py additionally drives each
detector over synthetic inputs, but those synthetic cases are proof that
the detector still detects; they never stand in for the real-repo run.)

1. ``find_dangling_md_citations`` -- W7a. Prose in source/test files cited
   ``T31-brief.md`` five times; that document has never existed in this
   repo, in any commit. A citation to a document nobody can open is an
   unfalsifiable claim: the reader assumes the reasoning was written down
   somewhere and stops asking. Every ``*.md`` filename appearing in a
   docstring or comment must name a document that exists on disk now.

2. ``find_unsupported_ticket_quotes`` -- W7b. A shipped test file quoted a
   sentence, attributed it to T-28 ("Per T-28's own escape valve (...)"),
   and used it to justify shipping a test that asserts its own acceptance
   is NOT met. No such text occurs anywhere in T-28's entry in
   docs/tickets.json. Quoted text attributed to a ticket must actually be
   in that ticket.

3. ``find_undisclosed_protected_changes`` -- W13/W18. Commit ``61d26de``
   is titled "chore: regenerate docs/TASKS.md" and carries 100 changed
   lines of ``.claude/scripts/harness_lib.py`` -- the harness that decides
   what any session is allowed to do. A commit whose message claims only a
   mechanical change must not silently rewrite a PROTECTED path.

Design notes that matter for trusting these:

* The prose scanners read ONLY docstrings and comments (via ``ast`` and
  ``tokenize`` for Python, leading-comment lines for shell/TS). Paths that
  code constructs at runtime -- ``tmp_path / "notes.md"`` and friends --
  are deliberately out of frame, because a test authoring a file in a
  temp dir is not making a claim about a document that exists. The defect
  class here is *prose asserting a source that isn't there*.

* Detector 3 derives its protected-path list by AST-parsing ``PROTECTED``
  out of ``.claude/scripts/harness_lib.py`` rather than copying it, so
  adding a protected path to the harness automatically widens this check.
  It parses rather than imports because harness_lib.py shells out to git
  at import time.

* Detector 2 recognises double-quoted quotations only (``"`` and the curly
  pair). Single quotes are not distinguishable from apostrophes in this
  prose, so a quotation marked with them is not checked -- a known gap,
  not a claim of completeness.
"""

from __future__ import annotations

import ast
import io
import json
import re
import subprocess
import tokenize
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
TICKETS_PATH = REPO_ROOT / "docs" / "tickets.json"
HARNESS_LIB = REPO_ROOT / ".claude" / "scripts" / "harness_lib.py"

# Trees whose prose makes claims about this project. Anything outside these
# is documentation, not source, and is not scanned.
PROSE_ROOTS = (
    "backend/src",
    "backend/tests",
    "evals",
    ".claude/scripts",
    ".claude/hooks",
    "portal/src",
)
_PY_SUFFIXES = {".py"}
_LINE_COMMENT_SUFFIXES = {".sh": "#", ".ts": "//", ".tsx": "//", ".js": "//"}
# The detector and its test cannot name the defects they detect -- the
# phantom document, the fabricated quote -- without citing them, so scanning
# them would make them permanently self-flagging. This is the one blind spot
# in the prose scan and it is deliberate: these two files ARE the check, and
# are reviewed as such.
SELF_REFERENTIAL = frozenset(
    {
        "backend/tests/plan/_citationlib.py",
        "backend/tests/plan/test_citation_integrity.py",
    }
)
_SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "worktrees",
    "dist",
}


@dataclass(frozen=True)
class Finding:
    """One detected defect.

    ``key()`` deliberately omits the line number: a finding must keep the
    same identity when unrelated edits move it up or down the file, or the
    baseline in known_citation_defects.json would churn on every commit.
    """

    check: str
    where: str  # repo-relative file path, or a commit sha
    subject: str  # the citation / quote / path that is the defect
    detail: str  # human-facing explanation, includes the line number

    def key(self) -> tuple[str, str, str]:
        return (self.check, self.where, self.subject)

    def __str__(self) -> str:
        return f"[{self.check}] {self.where}: {self.subject!r}\n    {self.detail}"


# --------------------------------------------------------------------------
# Prose extraction
# --------------------------------------------------------------------------


class Prose:
    """Docstring + comment text of one file, with char offsets mapped back
    to real line numbers.

    Two normalisations are applied while building the flat text:
    ``"-\\n"`` becomes ``"-"`` (a hyphenated line wrap -- ``docs/zendesk-``
    / ``runbook.md`` in backend/src/ingress/models.py is one real, current
    example) and every other newline becomes a space. Without the first,
    that citation reads as the non-existent ``runbook.md``.
    """

    def __init__(self, chunks: list[tuple[int, str]]) -> None:
        parts: list[str] = []
        line_of: list[int] = []
        for start_line, text in chunks:
            lineno = start_line
            i = 0
            while i < len(text):
                ch = text[i]
                if ch == "\n":
                    if parts and parts[-1] == "-":
                        pass  # hyphenated wrap: drop the newline entirely
                    else:
                        parts.append(" ")
                        line_of.append(lineno)
                    lineno += 1
                else:
                    parts.append(ch)
                    line_of.append(lineno)
                i += 1
            parts.append("\n")
            line_of.append(lineno)
        self.text = "".join(parts)
        self._line_of = line_of

    def line_at(self, offset: int) -> int:
        if not self._line_of:
            return 0
        return self._line_of[min(offset, len(self._line_of) - 1)]


def _python_prose(path: Path) -> Prose:
    src = path.read_text(encoding="utf-8", errors="replace")
    chunks: list[tuple[int, str]] = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        tree = None
    if tree is not None:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                chunks.append((node.lineno, node.value.value))
    comment_lines: list[tuple[int, str]] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                # Strip the marker. A "#" left mid-sentence turns a comment
                # block that wraps across lines into text that matches
                # nothing -- _planlib.py's quotation of T-14's decidable
                # rule spans three comment lines and was unmatchable.
                comment_lines.append((tok.start[0], tok.string.lstrip("#").strip()))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    chunks.extend(_merge_adjacent(comment_lines))
    chunks.sort(key=lambda c: c[0])
    return Prose(chunks)


def _merge_adjacent(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Consecutive comment lines are ONE piece of prose. Without this, an
    attribution whose ticket id and quotation sit on adjacent comment lines
    would be split by a chunk boundary and never seen -- and, worse, a
    ticket id at the end of one unrelated block could bind to a quotation
    at the start of the next."""
    blocks: list[tuple[int, list[str]]] = []
    prev_line = None
    for lineno, body in sorted(lines):
        if blocks and prev_line is not None and lineno == prev_line + 1:
            blocks[-1][1].append(body)
        else:
            blocks.append((lineno, [body]))
        prev_line = lineno
    return [(start, "\n".join(bodies)) for start, bodies in blocks]


def _line_comment_prose(path: Path, marker: str) -> Prose:
    lines: list[tuple[int, str]] = []
    for i, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith(marker):
            lines.append((i, stripped[len(marker) :].strip()))
    return Prose(_merge_adjacent(lines))


def iter_prose_files() -> list[Path]:
    out: list[Path] = []
    for rel in PROSE_ROOTS:
        base = REPO_ROOT / rel
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            # Relative to REPO_ROOT, never absolute: the repo itself can sit
            # under a path containing a skipped name (an agent worktree lives
            # at .claude/worktrees/<id>/), and matching absolute parts made
            # this scan silently return nothing.
            rel_parts = p.relative_to(REPO_ROOT)
            if any(part in _SKIP_DIRS for part in rel_parts.parts):
                continue
            if rel_parts.as_posix() in SELF_REFERENTIAL:
                continue
            if p.suffix in _PY_SUFFIXES or p.suffix in _LINE_COMMENT_SUFFIXES:
                out.append(p)
    return out


def extract_prose(path: Path) -> Prose:
    if path.suffix in _PY_SUFFIXES:
        return _python_prose(path)
    marker = _LINE_COMMENT_SUFFIXES[path.suffix]
    return _line_comment_prose(path, marker)


# --------------------------------------------------------------------------
# 1. Dangling markdown citations (W7a)
# --------------------------------------------------------------------------

CHECK_MD = "dangling-md-citation"

# A markdown filename as it appears in prose: optional leading dot (so
# ".claude/NEEDS_HUMAN.md" survives), then path-ish characters, then ".md".
# The lookbehind stops mid-path matches.
_MD_TOKEN = re.compile(r"(?<![\w\-/.])\.?[\w][\w.\-/]*\.md\b")


def existing_md_documents(root: Path | None = None) -> set[str]:
    """Every markdown document currently on disk, repo-relative, lowercased."""
    root = root or REPO_ROOT
    out: set[str] = set()
    for p in root.rglob("*.md"):
        if any(part in _SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        out.add(p.relative_to(root).as_posix().lower())
    return out


def md_citation_resolves(token: str, existing: set[str]) -> bool:
    """Resolution is deliberately generous: the check's contract is only
    "this document exists SOMEWHERE in the repo", never "this path is
    exactly right". Three tiers, each one a real pattern in this repo:

    * exact repo-relative path        -- ``docs/TASKS.md``
    * path suffix                     -- ``TASKS.md`` -> ``docs/TASKS.md``
    * basename, or basename after a
      hyphen (survives a line wrap)   -- ``runbook.md`` ->
                                         ``docs/zendesk-runbook.md``

    The generosity is the point: it keeps ``src/SPEC.md`` (a hypothetical
    ``git mv`` destination discussed in test_claim_ledger_integrity.py's
    docstring, which is not and must not be a real file) from being
    flagged, while a document that has never existed under any name --
    ``T31-brief.md`` -- still has nowhere to resolve to.
    """
    t = token.lower()
    if t.startswith("./"):
        t = t[2:]
    if not t:
        return False
    base = t.rsplit("/", 1)[-1]
    for existing_path in existing:
        if existing_path == t or existing_path.endswith("/" + t):
            return True
        existing_base = existing_path.rsplit("/", 1)[-1]
        if existing_base == base or existing_base.endswith("-" + base):
            return True
    return False


def find_dangling_md_citations(files: list[Path] | None = None) -> list[Finding]:
    existing = existing_md_documents()
    findings: dict[tuple[str, str, str], Finding] = {}
    for path in files if files is not None else iter_prose_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        prose = extract_prose(path)
        for m in _MD_TOKEN.finditer(prose.text):
            token = m.group(0)
            if md_citation_resolves(token, existing):
                continue
            f = Finding(
                check=CHECK_MD,
                where=rel,
                subject=token,
                detail=(
                    f"line {prose.line_at(m.start())}: prose cites {token!r}, "
                    f"but no markdown document with that path, path suffix or "
                    f"basename exists anywhere in the repo. Either the document "
                    f"was never written (the citation is unfalsifiable and the "
                    f"claim it supports is unsupported) or it was deleted and "
                    f"the reference is dangling."
                ),
            )
            findings.setdefault(f.key(), f)
    return sorted(findings.values(), key=lambda f: f.key())


# --------------------------------------------------------------------------
# 2. Quotes attributed to a ticket that the ticket does not contain (W7b)
# --------------------------------------------------------------------------

CHECK_QUOTE = "unsupported-ticket-quote"

# "T-28's own escape valve (\"...\")", "T-4's acceptance also requires \"...\"",
# "T-26's own non_goals (\"...\")". The link between the ticket id and the
# opening quote is capped at 60 characters so that a ticket id merely
# mentioned earlier in a paragraph does not get bound to an unrelated quote
# further down. Three tempering rules, each added because it removed a real
# false positive in this repo:
#   (1) the link may not contain another ticket id, so a quotation binds to
#       the NEAREST id before it. Without this, escalation_seam.py's "The
#       T-6 escalation-engine seam. ... T-5's non-goal is explicit: '...'"
#       attributed T-5's non-goal to T-6.
#   (2) the link may not cross a sentence boundary. Without this,
#       test_claim_format.py's "(T-31 acceptance 2/4). * v1 '...'"
#       attributed a v1 test name to T-31.
#   (3) the link may not contain a filename. Without this,
#       test_ingest_doc.py's "The pre-T-26 version of docs/INGEST.md's
#       confirmation step (step 4) read '...'" attributed INGEST.md's old
#       wording to T-26.
# A quotation lives inside ONE docstring or comment block: Prose emits a
# literal newline only at a chunk boundary, so excluding it here stops a
# quotation (and, below, an attribution) from spanning two of them.
_QUOTED_SPAN = re.compile(r"[\"“]([^\"“”\n]{1,800})[\"”]")
_TICKET_ID = re.compile(r"\bT-\d{1,3}\b")

# How much text may sit between the ticket id and the opening quote.
_MAX_LINK_CHARS = 60
# Rule (3): a filename in the lead-in means the quotation is attributed to
# that document, not to the ticket.
_FILE_REF = re.compile(r"[\w/.\-]+\.(?:md|json|ya?ml|py|sh|ts|tsx)\b")

# The link must contain one of these to count as an ATTRIBUTION rather than
# an incidental adjacency. Kept narrow on purpose: a quotation that is not
# claimed to be the ticket's own words is none of this check's business.
_ATTRIBUTION_CUES = re.compile(
    r"(?:['’]s\b|\bsays?\b|\bsaid\b|\bstates?\b|\brequires?\b|\brequired\b"
    r"|\bmandates?\b|\basks?\b|\basked\b|\bdemands?\b|\bnames\b|\bcalls\b"
    r"|\bacceptance\b|\bobjective\b|\bnon_goals?\b|\bnon-goals?\b|\bcontract\b"
    r"|\bwording\b|\btext\b)",
    re.IGNORECASE,
)

_MIN_QUOTE_CHARS = 20
_MIN_QUOTE_WORDS = 4


def _squash(s: str) -> str:
    """Collapse whitespace for display and for baseline keys, so a
    re-wrapped docstring does not change a finding's identity."""
    return re.sub(r"\s+", " ", s).strip()


def _normalize_for_quote_match(s: str) -> str:
    """Fold the cosmetic differences between a docstring quotation and the
    same sentence in the ticket: markdown emphasis, backticks, curly quotes
    and dashes, case, and any run of whitespace. Brackets are handled
    separately by ``_quote_variants`` -- they mean different things on the
    two sides of the comparison."""
    s = s.replace("‘", "'").replace("’", "'")
    s = s.replace("“", '"').replace("”", '"')
    s = s.replace("—", "-").replace("–", "-")
    s = re.sub(r"[`*_]", "", s)
    s = re.sub(r"\s+", " ", s)
    # Spacing around a slash is typography, not wording: _planlib.py quotes
    # T-14's "(pytest/npm/uv run pytest)" as "(pytest / npm / uv run
    # pytest)" while otherwise reproducing the rule exactly.
    s = re.sub(r"\s*/\s*", "/", s)
    return s.strip().lower()


def _quote_variants(segment: str) -> list[str]:
    """The forms a faithful quotation of a ticket may legitimately take.

    ``[...]`` inside a quotation is the scholarly marker for the quoting
    author's own clarifying insertion, so it is NOT part of what the source
    said: test_status_field.py quotes T-22's objective as "every ticket
    boundary since [T-14] has required ..." where the objective itself
    reads "Every ticket boundary since has required ...". That is an
    honest, correctly signposted insertion and must not be flagged.

    But brackets can also be the source's own characters, faithfully
    reproduced. So both readings are tried and either one acquits. The
    asymmetry is the point -- the ticket side is never rewritten, because
    a ticket's brackets are simply its words."""
    deleted = _normalize_for_quote_match(re.sub(r"\[[^\]]*\]", " ", segment))
    kept = _normalize_for_quote_match(segment.replace("[", "").replace("]", ""))
    return [v for v in dict.fromkeys([deleted, kept]) if v]


def _iter_strings(node: object) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for v in node.values() for s in _iter_strings(v)]
    if isinstance(node, list):
        return [s for v in node for s in _iter_strings(v)]
    return []


def ticket_text(ticket: dict) -> str:
    """Everything the ticket says, as one normalised blob.

    Built by walking the ticket's own string values rather than
    ``json.dumps``: the JSON punctuation that dumps adds (``[``, ``]``,
    ``{``, quotes) is not the ticket's language, and letting it into the
    blob made bracket handling silently delete whole acceptance arrays."""
    return _normalize_for_quote_match("   ".join(_iter_strings(ticket)))


def quote_segments(quote: str) -> list[str]:
    """Split a quotation on an author's elision marker and keep the segments
    substantial enough to be checkable. ``"If a case is genuinely
    unreachable ... say so plainly"`` is two claims about the source text,
    and both must hold."""
    raw = re.split(r"\.\.\.|…", quote)
    out = []
    for seg in raw:
        seg = seg.strip().strip(",;:. ")
        if len(seg) >= _MIN_QUOTE_CHARS and len(seg.split()) >= _MIN_QUOTE_WORDS:
            out.append(seg)
    return out


def load_tickets() -> list[dict]:
    return json.loads(TICKETS_PATH.read_text())["tickets"]


def iter_attributions(text: str) -> Iterator[tuple[str, str, int]]:
    """Yield ``(ticket_id, quote, tid_offset)`` for every quotation in
    ``text`` that is claimed to be a ticket's own words.

    Quotations are paired left to right FIRST, and only the text between
    one quotation and the next is searched for an attributing ticket id.
    Doing it the other way round -- scanning for a ticket id and then the
    next quote character -- let an id sitting INSIDE a quotation bind to
    the quotation's own closing quote, which made test_ingest_doc.py's
    quotation of an old INGEST.md step read as a quotation of T-0.
    """
    cursor = 0
    for span in _QUOTED_SPAN.finditer(text):
        lead_in = text[cursor : span.start()]
        cursor = span.end()
        ids = list(_TICKET_ID.finditer(lead_in))
        if not ids:
            continue
        last = ids[-1]  # (1) nearest id wins
        link = lead_in[last.end() :]
        if len(link) > _MAX_LINK_CHARS:
            continue
        if "\n" in link:  # (0) same docstring / comment block
            continue
        if ". " in link:  # (2) same sentence
            continue
        if _FILE_REF.search(link):  # (3) attributed to a document, not a ticket
            continue
        if not _ATTRIBUTION_CUES.search(link):
            continue
        yield last.group(0), span.group(1), cursor - len(span.group(0)) - 2


def find_unsupported_ticket_quotes(
    files: list[Path] | None = None, tickets: list[dict] | None = None
) -> list[Finding]:
    by_id = {t["id"]: t for t in (tickets if tickets is not None else load_tickets())}
    blobs = {tid: ticket_text(t) for tid, t in by_id.items()}
    findings: dict[tuple[str, str, str], Finding] = {}
    for path in files if files is not None else iter_prose_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        prose = extract_prose(path)
        for tid, quote, offset in iter_attributions(prose.text):
            if tid not in blobs:
                # A quotation attributed to a ticket that is not in the plan
                # at all -- the strongest possible form of this defect.
                f = Finding(
                    check=CHECK_QUOTE,
                    where=rel,
                    subject=f"{tid}: {_squash(quote)[:120]}",
                    detail=(
                        f"line {prose.line_at(offset)}: text is quoted and "
                        f"attributed to {tid}, which does not exist in "
                        f"docs/tickets.json at all."
                    ),
                )
                findings.setdefault(f.key(), f)
                continue
            missing = [
                seg
                for seg in quote_segments(quote)
                if not any(v in blobs[tid] for v in _quote_variants(seg))
            ]
            if not missing:
                continue
            f = Finding(
                check=CHECK_QUOTE,
                where=rel,
                subject=f"{tid}: {_squash(missing[0])[:120]}",
                detail=(
                    f"line {prose.line_at(offset)}: quoted as {tid}'s own "
                    f"words -- {_squash(quote)[:200]!r} -- but the segment "
                    f"{_squash(missing[0])[:160]!r} occurs nowhere in {tid}'s "
                    f"entry in docs/tickets.json. A ticket's contract cannot be "
                    f"widened by quoting language it does not contain."
                ),
            )
            findings.setdefault(f.key(), f)
    return sorted(findings.values(), key=lambda f: f.key())


# --------------------------------------------------------------------------
# 3. Commits whose message understates what they changed (W13/W18)
# --------------------------------------------------------------------------

CHECK_COMMIT = "undisclosed-protected-change"

# A subject that claims nothing but mechanical work. Anchored at the start:
# "harness: close the rename ... laundering routes" contains "rename" but
# does not CLAIM to be a rename, and must not be flagged.
_TRIVIAL_SUBJECT = re.compile(
    r"""^\s*(?:
        (?:chore|docs|style|ci|build)\s*(?:\([^)]*\))?\s*:
      | (?:re-?gen(?:erate)?|re-?sync|sync|update|bump|tidy|clean(?:up|\s+up)?
        |re-?format(?:ting)?|format(?:ting)?|whitespace|typos?|lint(?:ing)?
        |comments?|no-?op|nfc|touch\s+up|touch-up)\b
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Below this many changed lines in one protected file, a mismatch between
# message and diff is plausibly an oversight rather than a misdescription.
# (Measured: on this repo's history the flagged set is identical at 1.)
SUBSTANTIVE_LINES = 10

# Harness-written state: changes at every ticket boundary by design, so a
# "chore: sync harness state" commit that only touches these is telling the
# truth. Same list harness_lib.py exempts from its own integrity diff.
_STATE_PREFIXES = (".claude/claims/", ".claude/evidence/", ".claude/monitor/")

# Path components too generic to count as disclosing which file changed.
_GENERIC_COMPONENTS = {".claude", "docs", "src", "backend", "tests"}


def _harness_constant(name: str) -> list[str]:
    """Read a top-level list-of-string-literals constant out of
    harness_lib.py by AST, without importing it (it shells out to git at
    import time). Keeps this check bound to the harness's own idea of what
    is protected instead of a copy that can drift."""
    tree = ast.parse(HARNESS_LIB.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return [
                        e.value
                        for e in node.value.elts  # type: ignore[attr-defined]
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)
                    ]
    raise AssertionError(f"{name} not found in {HARNESS_LIB}")


def _regex_literal_prefix(raw: str) -> str:
    """The plain path prefix a harness_lib path regex anchors on, e.g.
    ``r"^\\.claude/scripts/.*$"`` -> ``".claude/scripts/"``."""
    s = raw.lstrip("^")
    s = re.sub(r"\.\*\$?$", "", s)
    s = s.rstrip("$")
    return s.replace("\\", "")


def substantive_protected_patterns() -> list[re.Pattern[str]]:
    """harness_lib.PROTECTED minus the harness's own state directories.

    ``.claude/claims/**`` and ``.claude/evidence/**`` are PROTECTED against
    agent Edit/Write, but the harness itself rewrites them at every ticket
    boundary -- a "chore: sync harness state" commit that touches only
    those is describing itself accurately."""
    pats = []
    for raw in _harness_constant("PROTECTED"):
        prefix = _regex_literal_prefix(raw)
        if any(prefix.startswith(state) for state in _STATE_PREFIXES):
            continue
        pats.append(re.compile(raw))
    return pats


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _disclosure_tokens(path: str) -> set[str]:
    """Strings whose presence in a commit message means the message did
    admit to touching this path."""
    out = {path.lower()}
    base = path.rsplit("/", 1)[-1].lower()
    out.add(base)
    if "." in base:
        out.add(base.rsplit(".", 1)[0])
    for comp in path.lower().split("/")[:-1]:
        if comp not in _GENERIC_COMPONENTS:
            out.add(comp)
    return {t for t in out if t}


def iter_commits() -> list[dict]:
    """Every commit reachable from HEAD, with its message and per-file
    changed-line counts. One git call."""
    sep = "\x1e"
    out = _git(
        "log",
        f"--format={sep}%H%x1f%s%x1f%b",
        "--numstat",
        "--no-renames",
        "HEAD",
    )
    commits = []
    for block in out.split(sep):
        if not block.strip():
            continue
        head, _, rest = block.partition("\n")
        sha, _, tail = head.partition("\x1f")
        subject, _, body_first = tail.partition("\x1f")
        body_lines = [body_first] if body_first else []
        files: list[tuple[str, int]] = []
        for line in rest.splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and (parts[0].isdigit() or parts[0] == "-"):
                add = int(parts[0]) if parts[0].isdigit() else 0
                dele = int(parts[1]) if parts[1].isdigit() else 0
                files.append((parts[2], add + dele))
            elif line.strip():
                body_lines.append(line)
        commits.append(
            {
                "sha": sha,
                "subject": subject,
                "body": "\n".join(body_lines),
                "files": files,
            }
        )
    return commits


def find_undisclosed_protected_changes(
    commits: list[dict] | None = None,
    min_lines: int = SUBSTANTIVE_LINES,
) -> list[Finding]:
    pats = substantive_protected_patterns()
    findings: dict[tuple[str, str, str], Finding] = {}
    for c in commits if commits is not None else iter_commits():
        if not _TRIVIAL_SUBJECT.match(c["subject"]):
            continue
        message = f"{c['subject']}\n{c['body']}".lower()
        for path, changed in c["files"]:
            if changed < min_lines:
                continue
            if not any(p.match(path) for p in pats):
                continue
            if any(tok in message for tok in _disclosure_tokens(path)):
                continue
            f = Finding(
                check=CHECK_COMMIT,
                where=c["sha"],
                subject=path,
                detail=(
                    f"commit {c['sha'][:7]} {c['subject']!r} describes only a "
                    f"mechanical change, but rewrites {changed} lines of the "
                    f"protected path {path} and never names it. Protected "
                    f"paths are the plan and the harness that enforces it; a "
                    f"reviewer skimming this subject has no reason to open "
                    f"the diff."
                ),
            )
            findings.setdefault(f.key(), f)
    return sorted(findings.values(), key=lambda f: f.key())


def commit_is_reachable(sha: str) -> bool:
    """A baselined commit finding is only applicable on a branch that
    actually contains the commit."""
    r = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "merge-base", "--is-ancestor", sha, "HEAD"],
        capture_output=True,
        text=True,
    )
    return r.returncode == 0


# --------------------------------------------------------------------------
# Review entry point
# --------------------------------------------------------------------------

ALL_CHECKS = (CHECK_MD, CHECK_QUOTE, CHECK_COMMIT)


def all_findings() -> list[Finding]:
    return (
        find_dangling_md_citations()
        + find_unsupported_ticket_quotes()
        + find_undisclosed_protected_changes()
    )


def main() -> int:
    findings = all_findings()
    for check in ALL_CHECKS:
        rows = [f for f in findings if f.check == check]
        print(f"\n=== {check}: {len(rows)} finding(s) ===")
        for f in rows:
            print(f"  {f}")
    print(f"\nTOTAL {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
