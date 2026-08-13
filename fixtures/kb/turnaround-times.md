---
slug: turnaround-times
title: Expected Turnaround Times by Stage
category: sop
keywords:
  - how long until I hear back about my sample
  - how long does this take
  - how long will this take
  - wait time
  - taking forever
  - when will I hear back
  - when will I get an update
  - how many weeks until results
  - what is the expected timeline
  - why is this taking so long
  - how much longer
  - eta
  - estimated completion date
---

## Why we quote per-stage windows, not one end-to-end number

Case timelines depend heavily on sample condition and, for genealogy,
on database match availability — factors that vary case to case. Instead
of a single misleading end-to-end estimate, we publish an expected
window for whichever stage a case is currently in. The case record's
`eta_weeks` field reflects this: it is the estimated number of weeks
remaining in the **current** stage, not the whole pipeline.

## Published windows

- **Intake: 1–2 weeks.** Paperwork verification and accessioning are
  administrative, not lab-bench, work, so this stage is short and
  predictable.
- **Extraction: 3–5 weeks.** Well-preserved samples extract toward the
  low end; degraded skeletal material or samples that need a second
  extraction attempt run toward the high end.
- **Sequencing: 3–8 weeks (typically 4–6).** Covers library prep, the
  sequencing run itself, and bioinformatic QC. Cases can queue for a
  sequencing run batch, which is the main source of variability.
- **Genealogy: 3–12 weeks.** By far the widest window. A case with
  close-relative database matches can resolve in a few weeks; a case
  with only distant or no matches can run the full window before the
  genealogy team concludes the phase. See
  `genealogy-limitations-and-expectations.md`.
- **Complete: 0 weeks.** The case has a delivered report; nothing is
  outstanding.

## How to read a case's eta_weeks

If a case is in extraction with `eta_weeks: 3`, extraction is expected
to finish in about 3 weeks — sequencing and (if elected) genealogy
still follow after that. `eta_weeks` is always specific to the current
stage, so do not add stages together when answering a requester;
simply state the current stage, its expected window, and remind them
that subsequent stages have their own windows above.

## When a case runs past its window

A case that has not moved stages or received an update in a long time
is not necessarily behind schedule — see `sample-failure-and-recourse.md`
for the most common cause (a failed extraction or sequencing QC check
that triggers a retry). If a requester is concerned their case appears
stalled well beyond the published window for its stage, that is a
specialist-review matter — see `escalation-and-specialist-requests.md`
rather than guessing at a revised date.

## Rush requests

Extraction and sequencing windows can be shortened for an additional
fee — see `requesting-a-rush.md` for eligibility and floors.
