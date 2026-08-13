# Fixtures

This directory is entirely **fictional content authored for the
othram-support-agent build**. "Meridian Forensic Genomics," its cases,
requesters, and policies are invented for testing and development.
Nothing here represents a real laboratory, a real case, or a real
person — names, emails, and case IDs are made up, and requester emails
use `example.com` / `example.org` on purpose.

## Contents

- **`cases.yaml`** — a seed set of ~30 fictional case records (one YAML
  mapping under the `cases:` key) covering every pipeline stage
  (`intake`, `extraction`, `sequencing`, `genealogy`, `complete`) plus
  edge cases: a just-submitted case, completed cases, stale
  mid-pipeline cases, requesters who own multiple cases, and cases
  missing both a DNA profile and accession photos.
- **`kb/*.md`** — 15 fictional support-knowledge-base documents (SOP,
  policy, and service articles), each with YAML front matter
  (`slug`, `title`, `category`) followed by markdown body content. These
  are the sole grounding source for the agent's process and policy
  answers — case facts always come from `cases.yaml` via typed lookups,
  never from these documents.
