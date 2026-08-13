One-time setup for this build. Do all of it before any coding.

1. Read docs/tickets.json.
2. For each ticket, TaskCreate:
   - subject: "<id>: <title>"  (the T-xxx prefix is required — hooks
     parse it)
   - description: the full contract — objective, acceptance, verify,
     scope, depends_on, non_goals — serialized as readable text.
   Record the returned task id for each ticket id.
3. After all tasks exist, wire dependencies: for each ticket with
   depends_on, TaskUpdate that task with addBlockedBy = the task ids
   of its dependencies.
4. Confirm: TaskList shows every ticket, with exactly T-0 unblocked
   (the graph's sole root).
5. Stop and report the ready set. Do not start a ticket in this turn.
