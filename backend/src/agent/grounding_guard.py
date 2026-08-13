"""Deterministic, no-LLM backstop for R9 on the one route DESIGN allows free
generation over: ``route == "kb"``.

Context (T-5 red-team finding, HIGH severity): ``compose``'s ``"kb"`` branch
sets ``draft = result.answer`` straight from an LLM call, and ``verify``'s
only gate on it is ``verifier_score``, a score produced by the SAME
``LLMClient`` instance that wrote the draft. A hostile/broken model can
therefore fabricate a case fact (a stage, an ETA, a DNA/photo-availability
claim, a case id) in its "KB answer" and simultaneously self-score that
answer 1.0 — nothing upstream of ``verify`` can tell the difference between
a genuinely grounded KB answer and a fabrication the judge rubber-stamped,
because both paths are "ask the LLM".

This module is the fix: a pure-Python function with no model call anywhere
in it, so a 1.0 groundedness score cannot buy its way past it. It does not
replace the groundedness judge (KB-content grounding is still useful
signal) — it adds an orthogonal, judge-independent check for exactly one
thing: does this free-generated text assert a claim shaped like a CASE
fact (an identifier, a pipeline stage, a turnaround estimate, a DNA/photo
availability statement) that this run cannot back with a tool result? R9
says a case fact must be traceable to a tool result in the run; on the
``"kb"`` route ``tool_results`` never contains a ``"case"`` key (only
``case_status``/``permission`` ever resolve one — see ``agent.nodes``), so
in the pipeline as it exists today, ANY case-fact-shaped claim detected
here is by construction untraceable and must force escalation. The
``tool_results`` parameter is still threaded through generically (rather
than hard-coding "always fail") so this guard keeps working correctly if a
future route ever *does* free-generate text alongside a resolved case.

``agent.nodes.verify`` is the caller, and it calls this unconditionally for
every ``"kb"``-route run, independent of (and in addition to) the
groundedness-score threshold check — see that function's docstring for why
the ordering matters.

HONESTY, per the fix's own requirement — read this before trusting the
guard blindly:

This is a SHAPE-based guard, not a semantic one. It recognizes case facts
by pattern (a case-id-shaped token, a stage word or one of a curated list
of paraphrases for it, a number-of-weeks claim near personalizing language,
a DNA/photo-availability-shaped sentence) — it does not understand meaning.
Two consequences follow, both deliberate trade-offs:

- False positives are possible and are the intended failure mode: text
  that merely *resembles* a case-fact claim (without being fabricated,
  or without being about a case at all) can trip the guard and force a
  needless escalation. That costs a human a look at a ticket that didn't
  need one — acceptable, and far cheaper than a fabricated fact reaching a
  customer.
- False negatives remain possible: a paraphrase creative enough to fall
  outside every pattern below (a curated stage-phrase list, a fixed set of
  ETA personalizing cues, a fixed set of availability/negation words) will
  not be caught. A model motivated to evade this specific guard, that knows
  its shape, could likely construct one. This is a backstop against the
  failure mode the red team actually demonstrated (a model confidently
  stating a specific, checkable fact in ordinary prose), not a proof that
  no fabrication can ever reach a customer. Closing that residual gap for
  real would need either a much larger curated/learned paraphrase corpus or
  a second, independent model call scoped narrowly to "does this text
  assert anything about a specific case" — itself an LLM, and so reintroduces
  exactly the self-grading risk this guard exists to avoid. That trade-off
  is a decision for a human, not something this ticket resolves silently.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from data import Case
from data.models import STAGES

# -- case identifiers, tolerant of format variation --------------------------

# Requires the literal "MFG" token (case-insensitive) immediately followed
# by exactly 8 digits, but tolerates ANY run of separator characters
# (space/dash/underscore/dot) between "MFG" and the digits, and between
# individual digits — so "mfg-2025-0734", "MFG 2025 0734", "mfg--2025-0734",
# and a regrouped "MFG-20-250734" all normalize to the same canonical id.
# Deliberately anchored on the "MFG" prefix (never on bare digit runs) so
# this can't false-positive on an unrelated 8-digit number (a date, a
# phone number) that happens to appear near other text.
_CASE_ID_LOOSE_RE = re.compile(r"MFG[\s\-_./]*((?:\d[\s\-_./]*){8,})", re.IGNORECASE)


def extract_case_ids_loose(text: str) -> set[str]:
    """Every case-id-shaped token in `text`, normalized to canonical
    ``MFG-XXXX-XXXX`` form regardless of case, separator choice, or digit
    grouping in the source text."""
    ids: set[str] = set()
    for match in _CASE_ID_LOOSE_RE.finditer(text):
        digits = re.sub(r"\D", "", match.group(1))
        if len(digits) >= 8:
            ids.add(f"MFG-{digits[:4]}-{digits[4:8]}")
    return ids


# -- pipeline stage claims, literal word or curated paraphrase ---------------

# Paraphrases seen (or reasonably anticipated) for each stage, matched as
# plain case-insensitive substrings. Curated, not exhaustive — see the
# module docstring's residual-risk note.
_STAGE_INDIRECT_PHRASES: dict[str, tuple[str, ...]] = {
    "intake": (
        "paperwork verification",
        "accessioning your sample",
        "logging in your sample",
        "checking in your kit",
    ),
    "extraction": (
        "isolating dna",
        "isolating your dna",
        "extracting dna",
        "extracting your dna",
        "pulling dna from",
    ),
    "sequencing": (
        "library prep",
        "sequencing your sample",
        "running your sample through the sequencer",
        "bioinformatic processing",
    ),
    "genealogy": (
        "building your family tree",
        "genetic genealogy research",
        "genealogy database",
        "genealogy databases",
        "searching genealogy",
        "genetic-genealogy databases",
    ),
    "complete": (
        "final report has been generated",
        "your report is finished",
        "case is complete",
        "all done with your case",
        "report is ready",
    ),
}


def extract_stage_claims(text: str) -> set[str]:
    """Every pipeline stage `text` mentions — either by literal stage word
    or by one of the curated indirect phrasings above.

    Raw extraction only, deliberately NOT gated on personalizing language:
    a stage word alone ("sequencing") is ambiguous — it's also ordinary KB
    vocabulary for describing the lab's process in general — so
    ``find_ungrounded_case_claims`` (the guard) additionally requires
    ``has_personalizing_cue`` before treating what this function finds as a
    claim about a specific case. Callers that already know `text` is about
    one specific case (e.g. the grounding test suite's structural
    assertions over a real ``case_status``-route reply) can and do use this
    function directly, ungated, since there personalization isn't in
    question."""
    lowered = text.lower()
    found: set[str] = {stage for stage in STAGES if stage in lowered}
    for stage, phrases in _STAGE_INDIRECT_PHRASES.items():
        if any(phrase in lowered for phrase in phrases):
            found.add(stage)
    return found


# -- ETA / turnaround claims, digits or spelled out --------------------------

_NUMBER_WORDS: dict[str, int] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "dozen": 12,
}

_NUMBER_WORD_ALTERNATION = "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True))

_WEEK_NUMBER_RE = re.compile(
    rf"\b(?:(\d+)|(?:a\s+)?({_NUMBER_WORD_ALTERNATION}))\s+(?:more\s+)?weeks?\b",
    re.IGNORECASE,
)

# A bare mention of a number-of-weeks, or of a stage name, is common in
# legitimate, non-case-specific KB prose (a general turnaround-time range —
# "Sequencing typically takes 3-8 weeks" — or an explanation of what a
# stage involves, mentioning its name). What distinguishes an actual claim
# about THIS customer's specific case is personalizing language alongside
# it — the template's own idiom ("N more week(s) ... in this stage",
# "Thanks for checking in on case ...") and reasonable equivalents ("your
# case", "your report", ...). ``find_ungrounded_case_claims`` gates both
# its stage check and its ETA check on this cue list so a generic KB
# explanation that happens to name a stage or a week-range is not itself
# treated as a case-fact claim — only text that ALSO sounds like it is
# talking about the customer's specific case is. (Case-id and DNA/photo-
# availability claims are NOT gated this way — see their own docstrings for
# why those two are unambiguous regardless of surrounding language.)
_PERSONALIZING_CUES = (
    "your case",
    "your report",
    "your sample",
    "your dna",
    "your profile",
    "this case",
    "in this stage",
    "more week",
    "remaining",
    "weeks left",
    "week left",
    "before your",
    "before you",
)


def has_personalizing_cue(text: str) -> bool:
    """Whether `text` contains language suggesting it is talking about a
    SPECIFIC customer's case, rather than describing the lab's process or
    policy in general — see ``_PERSONALIZING_CUES`` above."""
    lowered = text.lower()
    return any(cue in lowered for cue in _PERSONALIZING_CUES)


def extract_eta_weeks_claims(text: str) -> set[int]:
    """Every number-of-weeks figure `text` states as a personalized
    turnaround/ETA claim (digits or spelled out), or an empty set if no
    week-number appears, or none does alongside personalizing language."""
    if not has_personalizing_cue(text):
        return set()

    numbers: set[int] = set()
    for match in _WEEK_NUMBER_RE.finditer(text):
        if match.group(1):
            numbers.add(int(match.group(1)))
        elif match.group(2):
            numbers.add(_NUMBER_WORDS[match.group(2).lower()])
    return numbers


# -- DNA-profile / accession-photo availability claims, in free prose -------

_DNA_TERMS = ("dna profile", "genetic profile", "your dna")
_PHOTO_TERMS = ("accession photo", "case photo", "your photos", "your photo")

_NEGATIVE_AVAILABILITY_PHRASES = (
    "not yet available",
    "not available",
    "not ready",
    "not on file",
    "hasn't been",
    "has not been",
    "still pending",
    "still processing",
)
_POSITIVE_AVAILABILITY_WORDS = (
    "available",
    "ready",
    "on file",
    "complete",
    "generated",
)


# Splits on sentence-ending punctuation or a line break — good enough to
# separate the case-status template's own one-fact-per-line rendering
# ("DNA profile: not yet available.\nAccession photos: available.") into
# independent sentences, and ordinary prose into its independent claims.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def _sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]


def _claimed_availability(text: str) -> bool | None:
    """``True``/``False`` if `text` states a determinate availability claim
    (checking negation phrases first, since e.g. "not yet available"
    contains the substring "available"), or ``None`` if ambiguous/absent."""
    lowered = text.lower()
    if any(phrase in lowered for phrase in _NEGATIVE_AVAILABILITY_PHRASES):
        return False
    if any(word in lowered for word in _POSITIVE_AVAILABILITY_WORDS):
        return True
    return None


def _availability_claim(text: str, terms: tuple[str, ...]) -> bool | None:
    """``True``/``False`` if some SENTENCE of `text` mentions one of `terms`
    and states a determinate availability claim, or ``None`` if no sentence
    mentions any of `terms` at all.

    Scoped per-sentence deliberately: the case-status template states BOTH
    a DNA fact and a photos fact in the same body, on adjacent lines, and
    they can disagree (DNA not yet available, photos available already is
    exactly ``fixtures/cases.yaml``'s ``MFG-2025-0734``). Checking
    ``_claimed_availability`` over the whole body would let one fact's
    negation word ("not yet available") bleed into the other fact's
    claim — a real bug this scoping exists to prevent, not a hypothetical
    one (it broke ``assert_case_facts_trace_to`` against exactly that
    fixture during this fix's own test run)."""
    for sentence in _sentences(text):
        lowered = sentence.lower()
        if any(term in lowered for term in terms):
            polarity = _claimed_availability(sentence)
            if polarity is not None:
                return polarity
    return None


def extract_dna_availability_claim(text: str) -> bool | None:
    """``True``/``False`` if `text` asserts a DNA-profile-availability claim
    in prose (the template's own fixed sentence included), or ``None`` if
    `text` makes no such claim at all."""
    return _availability_claim(text, _DNA_TERMS)


def extract_photos_availability_claim(text: str) -> bool | None:
    """Same as ``extract_dna_availability_claim`` for accession photos."""
    return _availability_claim(text, _PHOTO_TERMS)


# -- the guard itself ---------------------------------------------------------


@dataclass(frozen=True)
class GuardViolation:
    """One case-fact-shaped claim the guard could not trace to this run's
    ``tool_results``."""

    kind: str  # "case_id" | "stage" | "eta" | "dna_availability" | "photos_availability"
    detail: str


def _resolved_case(tool_results: Mapping[str, Any]) -> Case | None:
    case = tool_results.get("case")
    return case if isinstance(case, Case) else None


def find_ungrounded_case_claims(
    text: str, tool_results: Mapping[str, Any] | None
) -> list[GuardViolation]:
    """Every case-fact-shaped claim in `text` that is not traceable to a
    field of ``tool_results["case"]`` (this run's resolved ``data.Case``
    tool result, if any) — empty if `text` makes no such claim, or every
    claim it makes matches that case exactly.

    On the only route that calls this today (``"kb"``), ``tool_results``
    never carries a ``"case"`` key at all (see the module docstring), so in
    practice any claim detected below is unconditionally a violation. The
    per-field comparison against a resolved case is kept anyway so the
    function stays correct if a future free-generation path ever does have
    one to check against, rather than hard-coding "always violation".
    """
    resolved_tool_results = tool_results or {}
    case = _resolved_case(resolved_tool_results)
    violations: list[GuardViolation] = []

    mentioned_ids = extract_case_ids_loose(text)
    allowed_ids = {case.case_id} if case is not None else set()
    leaked_ids = mentioned_ids - allowed_ids
    if leaked_ids:
        violations.append(
            GuardViolation(
                kind="case_id",
                detail=(
                    f"case id(s) {sorted(leaked_ids)} not traceable to this run's tool result"
                ),
            )
        )

    # Gated on `has_personalizing_cue` — see that function's docstring and
    # ``extract_stage_claims``'s: a bare stage word is ordinary KB
    # vocabulary (explaining what "sequencing" involves in general) unless
    # something in `text` also makes it sound like a claim about the
    # customer's specific case.
    if has_personalizing_cue(text):
        mentioned_stages = extract_stage_claims(text)
        allowed_stages = {case.stage} if case is not None else set()
        leaked_stages = mentioned_stages - allowed_stages
        if leaked_stages:
            violations.append(
                GuardViolation(
                    kind="stage",
                    detail=(
                        f"pipeline stage claim(s) {sorted(leaked_stages)} not traceable to "
                        "this run's tool result"
                    ),
                )
            )

    mentioned_etas = extract_eta_weeks_claims(text)
    allowed_etas = {case.eta_weeks} if case is not None and case.eta_weeks is not None else set()
    leaked_etas = mentioned_etas - allowed_etas
    if leaked_etas:
        violations.append(
            GuardViolation(
                kind="eta",
                detail=(
                    f"turnaround/ETA claim(s) of {sorted(leaked_etas)} week(s) not "
                    "traceable to this run's tool result"
                ),
            )
        )

    dna_claim = extract_dna_availability_claim(text)
    if dna_claim is not None:
        traceable = (
            case is not None
            and case.dna_profile_available is not None
            and dna_claim == bool(case.dna_profile_available)
        )
        if not traceable:
            violations.append(
                GuardViolation(
                    kind="dna_availability",
                    detail=(
                        f"DNA-profile availability claim ({dna_claim}) not traceable to "
                        "this run's tool result"
                    ),
                )
            )

    photos_claim = extract_photos_availability_claim(text)
    if photos_claim is not None:
        traceable = (
            case is not None
            and case.photos_available is not None
            and photos_claim == bool(case.photos_available)
        )
        if not traceable:
            violations.append(
                GuardViolation(
                    kind="photos_availability",
                    detail=(
                        f"accession-photo availability claim ({photos_claim}) not "
                        "traceable to this run's tool result"
                    ),
                )
            )

    return violations
