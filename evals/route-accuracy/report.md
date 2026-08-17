# Route-classification accuracy (W1-E3)

Generated 2026-08-17T01:12:00.125814+00:00 — model `claude-opus-5`.

Measured by calling the shipped `agent.nodes.classify` node against a live
`agent.llm.AnthropicLLMClient` over `evals/labeled_set.yaml`. No fakes, no
handed-in `Classification`. See `evals/route_accuracy.py`'s module docstring
for what is scored and what is diagnostic.

## Headline

- **Route accuracy: 1.0** (30/30 branch-route tickets)
- Labeled-set rows considered: 51
- Live model calls this run: 0 (cache hits: 51)
- Measured cost this run: 0.0 USD (0 in / 0 out tokens)

> Every verdict above was replayed from `/Users/hankholcomb/Documents/code_parent_folders/gauntlet_repos/cxforge/evals/route-accuracy/cache.json`, so this invocation spent nothing. The numbers are still live-model measurements — the cache stores what the model actually returned, keyed on the exact prompt, so a prompt edit invalidates it and forces a real re-measurement. Use `--refresh` to re-measure anyway.

## Confusion matrix — expected (rows) x predicted (columns)

| expected \ predicted | case_status | permission | kb | off_topic | error | recall |
|---|---|---|---|---|---|---|
| **case_status** | 10 | 0 | 0 | 0 | 0 | 1.0 |
| **permission** | 0 | 5 | 0 | 0 | 0 | 1.0 |
| **kb** | 0 | 0 | 10 | 0 | 0 | 1.0 |
| **off_topic** | 0 | 0 | 0 | 5 | 0 | 1.0 |

## Per-route precision / recall / F1

| route | support | precision | recall | F1 |
|---|---|---|---|---|
| case_status | 10 | 1.0 | 1.0 | 1.0 |
| permission | 5 | 1.0 | 1.0 | 1.0 |
| kb | 10 | 1.0 | 1.0 | 1.0 |
| off_topic | 5 | 1.0 | 1.0 | 1.0 |

## Diagnostic — `expected_route: escalate` tickets

`classify` cannot emit `escalate` (its schema is `agent.state.ClassifyRoute`), so
these are NOT scored against the headline. What matters is whether the branch it
picks can still detect the escalation condition.

- Escalate-labeled tickets: 21
- Route-dependent subset: 8 (accuracy 0.75)

Route distribution `classify` chose for escalate-labeled tickets:

| classify route | count |
|---|---|
| case_status | 7 |
| kb | 5 |
| off_topic | 7 |
| permission | 2 |

### Route-dependent misses — these tickets would NOT escalate

| ticket | required route | classify chose | expected reasons |
|---|---|---|---|
| `esc-low_confidence-verifier_failure-exact-date-01` | kb | case_status | low_confidence |
| `esc-low_confidence-verifier_failure-summed-timeline-01` | kb | case_status | low_confidence |
