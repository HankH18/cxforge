---
slug: sample-failure-and-recourse
title: What Happens When a Sample Fails
category: sop
keywords:
  - the lab said my swab didn't work, what happens now
  - my sample failed
  - the test didn't work
  - what happens if my sample fails
  - lab couldn't get a usable profile
  - my extraction failed, now what
  - the sequencing didn't work
  - is my sample ruined
  - what if there's no viable path forward
  - do I need to send a new sample because it failed
---

## Where "failure" can happen

A sample can fail to produce usable results at two points in the
pipeline: **extraction** (the DNA extract itself fails quality control)
or **sequencing** (a technically acceptable extract still fails to
produce a clean, usable profile — rarer, but possible with severely
degraded material). Both are covered here; extraction failure detail
and the re-extraction attempts themselves are in
`sample-quality-and-reextraction.md`.

## Extraction failure

Covered fully in `sample-quality-and-reextraction.md`: up to two free
re-extraction attempts are made automatically. The case stays in the
extraction stage, `dna_profile_available` stays `false`, and
`eta_weeks` reflects the remaining re-extraction work, not a delay.

## Sequencing failure

If a sequencing run does not yield a usable profile from an
otherwise-acceptable extract, the sequencing team reruns the sample on
the next available sequencing batch at no additional charge, one time.
If the rerun also fails, the case is returned to the extraction team to
assess whether the original extract is usable at all or whether a fresh
extraction (and, if needed, a new sample) is required — at that point
it follows the same path as an extraction failure.

## Total failure — no viable path forward

If every extraction attempt and the sequencing rerun both fail to
produce a usable profile, and a replacement sample is unavailable or
also fails, we cannot generate a DNA profile for the case on the
currently available material. The case is flagged for specialist
review rather than closed automatically — this determination should
never be communicated to a requester by the agent directly; escalate
per `escalation-and-specialist-requests.md` so a specialist can deliver
that news and discuss options (e.g., waiting for updated extraction
methods, or closing the case).

## Effect on billing

Sequencing-fee refund eligibility for a total failure is covered in
`refund-policy.md`; the agent should not quote a refund amount, only
point to that policy and escalate.

## Talking to requesters mid-failure

While a case is still within its normal re-extraction or resequencing
attempts, describe this as expected process, not a failure requiring
apology or escalation — see `sample-quality-and-reextraction.md`. Only
a total, no-viable-path-forward outcome is an escalation trigger.
