"""Citation integrity: a docstring/comment may not cite a source that does not exist.

Two failures of this kind reached HEAD and were load-bearing for a ticket close, which
is why this is a plan test and not a style nit:

  1. `backend/tests/hooks/test_close_unattributed_claim_gap.py` justified NOT meeting
     T-28 acceptance 1 by quoting "T-28's own escape valve" -- a sentence that appears
     nowhere in docs/tickets.json, or anywhere else in the repo. The quotation marks
     did the persuading; there was no source behind them.
  2. `backend/tests/hooks/conftest.py` and `test_scope_guard_append_only.py` cited
     "T31-brief.md" five times as an authoritative summary of the v1->v2 migration.
     No file matching *brief* has ever existed in this repository's git history.

Both are the same defect: prose that borrows the authority of a citation without the
citation being checkable. A reader (human or agent) who trusts the quotation marks
inherits a false premise. These checks make the claim mechanically falsifiable.

WHAT IS SCANNED, AND WHY ONLY THAT

Only PROSE -- module/class/function docstrings (via `ast`) and `#` comments (via
`tokenize`) -- is scanned. Ordinary string literals are deliberately excluded, because
in this repo a `.md` path inside code is usually a deliberately NON-existent test
input, not a citation: `test_scope_guard.py` passes "docs/zendesk-runbook-v2.md" and
"docs/agent-design.md" to the guard precisely to assert a deny for a file that is not
in scope. Flagging those would be wrong -- they are data, not claims about reality.
Restricting to prose is what makes this check low-false-positive rather than a
grep that has to be babysat with an ignore list.

Prose is de-wrapped before matching (a `-` at end-of-line rejoined to the next line),
because a hard-wrapped docstring splits real references: `backend/src/ingress/models.py`
wraps "docs/zendesk-" / "runbook.md" across two lines, and a naive scan reports the
non-existent "runbook.md".

NOT A STYLE CHECK. Neither test cares about formatting, only about whether the thing
being pointed at is real. An unquoted paraphrase ("T-28 allows reporting an
unreachable case") is untouched by these tests; it makes no verbatim claim. Putting
quotation marks around it is what invites the check.
"""

from __future__ import annotations

import ast
import io
import json
import re
import tokenize
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
TICKETS_PATH = REPO_ROOT / "docs" / "tickets.json"

# Trees whose prose makes claims about this repo. Excludes vendored/generated trees.
SCAN_ROOTS = ("backend/src", "backend/tests", "evals", ".claude/scripts")

# Directories that never contain first-party documentation.
_SKIP_DIRS = {".git", ".venv", "node_modules", "site-packages", "worktrees", "__pycache__"}

# This module is the one file that must be able to WRITE the bad citations verbatim:
# its docstrings quote "T31-brief.md" and the fabricated T-28 sentence as the
# counterexamples it exists to catch. Scanning itself would make it permanently red for
# naming its own subject matter -- the same exemption a linter gives its own fixtures.
# Nothing else is exempt, and this is a single self-reference, not a suppression list.
_SELF = "backend/tests/plan/test_citation_integrity.py"


# ---------------------------------------------------------------------------
# Prose extraction
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Prose:
    """One docstring or comment, with enough location to name it in a failure."""

    path: str  # repo-relative
    line: int
    text: str  # de-wrapped, whitespace-collapsed


def _iter_py_files() -> list[Path]:
    out: list[Path] = []
    for root in SCAN_ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.py")):
            rel = p.relative_to(REPO_ROOT)
            if _SKIP_DIRS & set(rel.parts):
                continue
            if rel.as_posix() == _SELF:
                continue
            out.append(p)
    return out


def _dewrap(text: str) -> str:
    """Rejoin hyphen-wrapped tokens, then collapse whitespace to single spaces.

    A docstring hard-wrapped at 88 columns can split a path or a quoted sentence
    across lines. Collapsing whitespace makes a quote comparable to the single-line
    JSON it was taken from; rejoining `-\\n` makes "docs/zendesk-\\nrunbook.md" read as
    the real "docs/zendesk-runbook.md" instead of a phantom "runbook.md".
    """
    text = re.sub(r"-\n[ \t]*(?=\w)", "-", text)
    return re.sub(r"\s+", " ", text).strip()


def collect_prose() -> list[Prose]:
    found: list[Prose] = []
    for path in _iter_py_files():
        rel = str(path.relative_to(REPO_ROOT))
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src, filename=str(path))
        except SyntaxError:  # pragma: no cover - repo does not contain unparseable py
            continue
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    line = node.body[0].lineno if getattr(node, "body", None) else 1
                    found.append(Prose(rel, line, _dewrap(doc)))
        try:
            for tok in tokenize.generate_tokens(io.StringIO(src).readline):
                if tok.type == tokenize.COMMENT:
                    found.append(Prose(rel, tok.start[0], _dewrap(tok.string.lstrip("#"))))
        except (tokenize.TokenError, IndentationError):  # pragma: no cover
            pass
    return found


# ---------------------------------------------------------------------------
# Check 1: every .md file named in prose exists
# ---------------------------------------------------------------------------

# A repo-relative-looking markdown path. The leading lookbehind stops a match from
# starting in the middle of a longer path (and so also skips URLs, whose "/" precedes
# the filename) -- under-detecting a URL is the safe direction for a fail-the-build test.
_MD_REF = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]*[\w-]\.md)\b")


def _repo_md_files() -> tuple[set[str], set[str]]:
    """(repo-relative paths, basenames) of every markdown file in the working tree."""
    rels: set[str] = set()
    names: set[str] = set()
    for p in REPO_ROOT.rglob("*.md"):
        parts = p.relative_to(REPO_ROOT).parts
        if _SKIP_DIRS & set(parts):
            continue
        rels.add("/".join(parts))
        names.add(p.name)
    return rels, names


def test_every_markdown_file_cited_in_prose_actually_exists() -> None:
    """Fails on the fabricated "T31-brief.md" citations.

    A reference resolves if it names a real repo-relative path OR if its basename
    matches a real markdown file anywhere in the tree. The basename fallback is
    deliberate: prose legitimately writes "TASKS.md", "README.md" or
    "harness-protocol.md" without a directory, and demanding a full path would flag
    dozens of correct references. It costs nothing here -- the defect this catches is
    a file that exists under NO name, not one filed in an unexpected directory.
    """
    rels, names = _repo_md_files()
    assert len(rels) > 20, f"markdown inventory looks broken ({len(rels)} files found)"

    checked = 0
    bad: list[str] = []
    for prose in collect_prose():
        for m in _MD_REF.finditer(prose.text):
            ref = m.group(1)
            checked += 1
            if ref in rels or ref.rsplit("/", 1)[-1] in names:
                continue
            excerpt = prose.text[max(0, m.start() - 70) : m.end() + 70]
            bad.append(
                f"{prose.path}:{prose.line} cites {ref!r} -- no such file"
                f"\n      ...{excerpt}..."
            )

    # Non-vacuity: this repo's prose is dense with real .md references. If the scanner
    # stops finding them, the test is no longer evidence of anything.
    assert checked > 40, f"only {checked} markdown references found -- scanner is broken"

    assert not bad, (
        "prose cites markdown files that do not exist. A citation borrows authority; "
        "if the file is not there, the sentence is asserting something unverifiable. "
        "Either point at the real file or state the fact directly, without a citation.\n\n  "
        + "\n  ".join(bad)
    )


# ---------------------------------------------------------------------------
# Check 2: text quoted and attributed to a ticket really is in that ticket
# ---------------------------------------------------------------------------

# Attribution forms that assert VERBATIM ticket text. Deliberately a closed list:
# possessive ("T-28's own escape valve"), a reporting verb ("T-13 says"), or a named
# ticket field ("T-7 acceptance:"). A bare mention ("see T-31") attributes nothing and
# is not matched.
_ATTRIBUTION = re.compile(
    r"\b(T-\d+)(?:'s(?:\s+own)?\b"
    r"|\s+(?:says|said|states|requires|reads)\b"
    r"|\s+(?:acceptance|objective|non-goals?|contract|verify|scope)\b)",
    re.IGNORECASE,
)

# Anything that re-points the sentence at a DIFFERENT source between the attribution
# and the quote: another ticket, a design document, or a filename. If one of these
# intervenes, the quote is no longer plausibly the attributed ticket's words.
_COMPETING_SOURCE = re.compile(
    r"T-\d+|DESIGN|SPEC|OBSM|AUDIT|\.md\b|\.py\b|\.sh\b|\.json\b|\.yaml\b"
)

# A sentence boundary between the attribution and the quote means the quote belongs to
# a later sentence, e.g. test_scope_guard_append_only.py's "T-13's ... ledger no longer
# exists ... Its replacement is not "a stricter append-only rule"" -- a scare quote, not
# a citation of T-13.
_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")

# How far after the attribution a quote may start and still be part of the same claim.
# Every genuine citation in this repo has a gap under 45 characters.
_MAX_GAP = 100

# Below this, a quoted span is a term of art ("queued", "**", "block"), not a citation.
_MIN_QUOTE_LEN = 25

_QUOTE_CHARS = '"“”'


def _quoted_spans(text: str) -> list[tuple[int, str]]:
    """(start_index_of_opening_quote, inner_text) for quotes paired left to right.

    Pairing sequentially -- 1st with 2nd, 3rd with 4th -- rather than searching for
    `"..."` anywhere matters: a greedy search happily starts at a CLOSING quote and
    swallows the ordinary prose that follows it, manufacturing "quotes" nobody wrote.
    That single bug accounted for every false positive in the first draft of this test.
    """
    positions = [i for i, ch in enumerate(text) if ch in _QUOTE_CHARS]
    return [
        (positions[i], text[positions[i] + 1 : positions[i + 1]])
        for i in range(0, len(positions) - 1, 2)
    ]


def _normalize(s: str) -> str:
    """Reduce a quote and its source to a comparable form.

    Strips markdown emphasis and code formatting the CITER added (`**bold**`,
    ``backticks``) -- docs/tickets.json contains neither, so leaving them in would fail
    honest citations such as test_scope_guard_fail_closed.py's quotation of T-27
    acceptance 2. Also drops trailing sentence punctuation, since a quotation
    conventionally absorbs the period that ends the sentence containing it.
    """
    s = s.replace("`", "").replace("**", "").replace("’", "'")
    s = re.sub(r"\s+", " ", s)
    return s.strip().strip(" .,;:").lower()


# Elision markers: "..." / "…" for omitted text, and "[...]" square brackets for an
# editorial insertion (test_status_field.py writes "every ticket boundary since [T-14]"
# to supply an antecedent T-22's own sentence left implicit). Both are legitimate
# quotation practice, so both are treated as wildcards -- the surviving fragments must
# still appear, in order, in the real ticket.
_ELISION = re.compile(r"\.\.\.|…|\[[^\]]*\]")


def _ticket_corpus(ticket: dict) -> str:
    """Every string anywhere in the ticket, normalized, joined by a separator that
    cannot be spanned accidentally -- so a "quote" cannot be assembled out of the tail
    of one acceptance clause and the head of the next."""
    parts: list[str] = []

    def walk(value: object) -> None:
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)

    walk(ticket)
    return _normalize(" ␟ ".join(parts))


def _occurs_in_order(quote: str, corpus: str) -> bool:
    segments = [seg for seg in (_normalize(s) for s in _ELISION.split(quote)) if seg]
    if not segments:
        return True
    cursor = 0
    for seg in segments:
        found = corpus.find(seg, cursor)
        if found < 0:
            return False
        cursor = found + len(seg)
    return True


def test_text_quoted_from_a_ticket_actually_appears_in_that_ticket() -> None:
    """Fails on the fabricated "T-28's own escape valve" quotation.

    Only quotations that are explicitly attributed to a ticket AND sit in the same
    clause as the attribution are checked -- see _ATTRIBUTION, _COMPETING_SOURCE and
    _SENTENCE_END for the three filters that keep scare quotes and quotations of other
    sources out. Elisions and editorial brackets are honoured, so a legitimately
    shortened quote still passes.
    """
    tickets = {t["id"]: t for t in json.loads(TICKETS_PATH.read_text())["tickets"]}
    corpora = {tid: _ticket_corpus(t) for tid, t in tickets.items()}

    verified = 0
    bad: list[str] = []
    for prose in collect_prose():
        for am in _ATTRIBUTION.finditer(prose.text):
            tid = am.group(1).upper()
            for qstart, quote in _quoted_spans(prose.text):
                if qstart < am.end():
                    continue
                gap = prose.text[am.end() : qstart]
                if len(gap) > _MAX_GAP:
                    break
                if _SENTENCE_END.search(gap) or _COMPETING_SOURCE.search(gap):
                    break
                if len(quote.strip()) < _MIN_QUOTE_LEN:
                    break
                if tid not in corpora:
                    bad.append(
                        f"{prose.path}:{prose.line} attributes a quote to {tid}, "
                        f"which is not a ticket in docs/tickets.json\n      {quote.strip()!r}"
                    )
                elif _occurs_in_order(quote, corpora[tid]):
                    verified += 1
                else:
                    bad.append(
                        f"{prose.path}:{prose.line} quotes {tid} as saying:\n"
                        f"      {quote.strip()!r}\n"
                        f"      -- that text is not in {tid}"
                    )
                break  # only the first quote after an attribution is the cited one

    # Non-vacuity: several docstrings in this repo quote their ticket correctly. If the
    # matcher stops recognising ANY of them it has broken open, and "no failures" would
    # mean nothing.
    assert verified >= 5, (
        f"only {verified} ticket quotations verified -- the matcher is no longer "
        "recognising known-good citations, so a pass proves nothing"
    )

    assert not bad, (
        "prose puts words in a ticket's mouth that the ticket does not contain. A "
        "quotation attributed to docs/tickets.json is a claim about an authoritative, "
        "read-only document; if the words are not there, drop the quotation marks and "
        "make the argument in your own voice.\n\n  " + "\n  ".join(bad)
    )
