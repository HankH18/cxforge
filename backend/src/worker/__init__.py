"""Background job execution — the seam that connects webhook ingress to the
agent graph (ADR-002, `docs/BUILD-PLAN.md` §3 Track A).

Until W1-A, `backend/src` contained **zero** invocations of
`agent.graph.run_agent`: the webhook validated, deduped, returned 202, and
the run never started. This package is the missing half.

    POST /webhooks/zendesk ──► tickets_seen INSERT ──► enqueue TicketJob
                                                              │
                                              Redis `cxforge:jobs`
                                                              │
                              arq worker ──► run_ticket ──► run_agent(...)

- `jobs.py`     — `TicketJob`, the frozen job payload (DESIGN §1.1).
- `settings.py` — Redis URL / queue name / task name, read from env.
- `queue.py`    — the enqueue seam ingress depends on.
- `main.py`     — the arq `WorkerSettings` the worker container runs:
                  `arq worker.main.WorkerSettings`.
"""
