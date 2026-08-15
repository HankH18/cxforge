#!/usr/bin/env python3
"""Render docs/TASKS.md FROM docs/tickets.json + derived status. Never hand-edit TASKS.md."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness_lib import ROOT, load_tickets, status
d = load_tickets()
L = ["# Task Graph (GENERATED from tickets.json — do not hand-edit)", ""]
for t in d["tickets"]:
    L += [f"### {t['id']}: {t['title']}  `[{status(t['id'])}]`",
          f"- **Objective**: {t['objective']}",
          f"- **Acceptance**: " + " ".join(f"{i+1}) {a}" for i, a in enumerate(t["acceptance"])),
          f"- **Verify**: `{t['verify']}`",
          f"- **Scope**: `{', '.join(t['scope'])}`",
          f"- **Depends on**: {', '.join(t['depends_on']) or 'none'} · **parallel_safe**: {str(t.get('parallel_safe', False)).lower()}",
          f"- **Non-goals**: {'; '.join(t.get('non_goals', []))}", ""]
open(os.path.join(ROOT, "docs", "TASKS.md"), "w").write("\n".join(L))
print("docs/TASKS.md regenerated")
