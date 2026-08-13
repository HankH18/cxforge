---
slug: sample-quality-and-reextraction
title: Sample Quality and Re-Extraction Policy
category: policy
keywords:
  - why is my extraction taking so long
  - my extraction failed, will you try again
  - re-extraction
  - second attempt at extraction
  - low DNA yield
  - degraded sample retry
  - why did my extract fail QC
---

## Why extractions fail QC

Every DNA extract is checked for yield (how much DNA was recovered)
and fragment length (how intact it is) before it is approved to move
to sequencing. Skeletal and tissue evidence, especially remains
exposed to heat, moisture, or time, frequently yields low or highly
fragmented DNA. A failed QC check is a normal, expected part of
forensic casework, not a sign of an error by our lab or the submitting
agency.

## Re-extraction policy

If an extract fails QC, the extraction team automatically attempts a
second extraction from a different portion of the same submitted
sample — this does not require the requester to do anything, and it
does not incur an additional charge. If the second attempt also fails,
a third and final re-extraction attempt is made using an alternate
extraction chemistry better suited to heavily degraded material. In
total, a case receives **up to two free re-extraction attempts** (three
extraction attempts overall) on the originally submitted material
before we consider it exhausted.

## When a new sample is required

If all three extraction attempts fail to produce a sequenceable
extract, the case cannot proceed on the current material. In that
event:

- For evidentiary samples, we contact the submitting agency to discuss
  whether additional skeletal material is available (e.g., a different
  bone element, which often yields better results than a repeat
  attempt on the same element).
- For reference samples, we contact the family member directly to
  arrange a replacement buccal swab.

This outcome is covered in more detail, including cost impact, in
`sample-failure-and-recourse.md` and `refund-policy.md`.

## Effect on `dna_profile_available`

A case remains in the extraction stage, with `dna_profile_available:
false`, for as long as re-extraction attempts are in progress. This
field only becomes `true` once a qualifying extract has been
successfully sequenced into a profile in the sequencing stage — a
case cannot show an available DNA profile while still in extraction.

## Talking to requesters about failed extractions

A requester asking why their case has not moved out of extraction
should be told, in plain terms, that the sample required a
re-extraction attempt due to normal degradation, that this is expected
for the sample type, and roughly where in the up-to-three-attempt
process the case stands if that information is available. Do not
characterize a re-extraction as an error, a lost sample, or grounds for
an apology on the lab's behalf — it is standard procedure.
