---
slug: pipeline-stages-overview
title: The Five-Stage Case Pipeline
category: sop
keywords:
  - what stage is my case in
  - what happens at each stage
  - process overview
  - what are the steps in the process
  - where is my case in the pipeline
  - what happens after intake
  - what's next for my case
---

## Overview

Every case at Meridian Forensic Genomics moves through five stages, in
order: **intake**, **extraction**, **sequencing**, **genealogy**, and
**complete**. A case's current stage tells you exactly what work is
happening on it right now and what remains. Support staff and the
support agent should always answer "where is my case" questions in
terms of these five stages, not internal lab jargon.

## Intake

Intake begins the moment a sample or case file is logged. Our
accessioning team confirms chain-of-custody paperwork, verifies the
sample type (skeletal, tissue, or reference buccal swab), assigns the
case ID, and enters the case into the tracking system. No lab work
happens yet. Intake is typically the shortest stage — see
`turnaround-times.md` for the current window.

## Extraction

The extraction team isolates DNA from the submitted material. For
degraded or environmentally exposed skeletal remains this is often the
most technically demanding stage, since bone and tooth samples require
specialized demineralization protocols. Every extract goes through a
quality-control check (yield and fragment-length assessment) before it
is approved to move to sequencing. See `sample-quality-and-reextraction.md`
for what happens when an extract fails QC.

## Sequencing

Approved extracts go into library preparation and massively parallel
sequencing, followed by bioinformatic processing to generate a
forensic-grade SNP profile. This is where `dna_profile_available`
becomes `true` on a case — the profile is the primary lab deliverable
of this stage. See `dna-profiles-and-genealogy-research.md` for what a
profile actually contains.

## Genealogy

If genealogy research was elected for the case, our genealogy team
uses the completed DNA profile to search opt-in consumer genetic
genealogy databases for relative matches, then builds family trees to
generate investigative leads on the identity of the case subject.
Genealogy is the most variable stage in duration because it depends on
database match density, not lab throughput — see
`genealogy-limitations-and-expectations.md`.

## Complete

Once all elected work is finished, the case moves to complete, the
final report package is generated, and `eta_weeks` drops to 0. See
`report-delivery-and-formats.md` for what the requester receives and
how to get another copy.

## Answering stage questions

When a requester asks "what stage is my case in" or "what's happening
right now," name the stage and describe only the activity for that
stage from this document — never speculate about work in a later
stage that has not started.
