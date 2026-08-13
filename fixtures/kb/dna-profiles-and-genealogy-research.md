---
slug: dna-profiles-and-genealogy-research
title: What a DNA Profile Is and What Genealogy Research Involves
category: sop
keywords:
  - what is a DNA profile
  - what does genealogy research actually involve
  - how do you find relatives from DNA
  - what is a SNP profile
  - how does genetic genealogy work
  - what can the DNA profile tell you
---

## What a DNA profile is

A DNA profile, in our pipeline, is a forensic-grade genome-wide SNP
(single-nucleotide polymorphism) profile generated during the
sequencing stage. Unlike a standard forensic STR profile used for
database matching against known offenders, a SNP profile captures
enough genetic markers to be searched against consumer genetic
genealogy databases for relatives — including distant relatives — of
the case subject. A case's `dna_profile_available` field turns `true`
the moment sequencing successfully produces this profile; it is the
sequencing stage's primary output.

## What the profile can and cannot tell us on its own

The profile alone identifies genetic relationships, not a name. It
cannot, by itself, tell us who the case subject is. Turning a profile
into an identity is the job of the genealogy stage, described below.

## What genealogy research involves

If genealogy work is elected for a case, our genealogy team uploads
the profile to opt-in genetic genealogy databases (services whose
users have consented to law-enforcement and forensic matching) and
reviews the resulting list of genetic relative matches. From there,
genealogists:

1. Assess match strength (close relative vs. distant cousin) to
   estimate how many generations separate the match from the case
   subject.
2. Build out family trees from those matches using public records,
   obituaries, and historical documents.
3. Triangulate across multiple matches to narrow the family tree
   toward a set of candidate identities for the case subject.
4. Deliver an investigative lead report to the requesting agency —
   candidate names for further investigation, not a certified
   identification.

## Confirmatory testing

Genealogy output is a lead, not a legal identification. Any candidate
identity produced by genealogy research must be confirmed through
traditional forensic means (e.g., a direct STR comparison against a
known reference, or dental/medical record comparison) before it can be
treated as a confirmed identification. Our report package makes this
distinction explicit — see `report-delivery-and-formats.md`.

## Role of case photography

Accession photographs (`photos_available`) document the physical
evidence and support the chain-of-custody record; they are not used as
part of the genetic analysis itself, but photos may be referenced
alongside dental or skeletal characteristics during confirmatory review
of a genealogy lead.

## Setting expectations

Because outcomes depend on database match density, not every case
resolves — see `genealogy-limitations-and-expectations.md` before
telling a requester what to expect from this stage.
