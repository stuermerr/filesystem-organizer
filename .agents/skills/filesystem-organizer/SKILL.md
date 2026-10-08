---
name: filesystem-organizer
description: Orchestrate safe consolidation and reorganization of an explicitly selected, stable filesystem collection through this repository's deterministic CLI. Use for scan-only analysis, consolidation, or recovery when the Selected Backup Root remains quiescent. Exclude continuous organization of active folders, generic Downloads cleanup, semantic categorization, and autonomous file management.
---

# Filesystem Organizer

Treat the CLI as the sole execution and safety boundary. Invoke its public commands and explain their artifacts; keep the Selected Backup Root unchanged and leave Run Workspace SQLite state to the CLI.

## Start

1. Read [privacy.md](references/privacy.md) and disclose its metadata boundary before the first command.
2. Classify the request as scan-only, reorganization, recovery, or out of scope.
3. For scan-only or reorganization, read [workflow.md](references/workflow.md) and follow the matching branch.
4. For recovery of existing work, read [recovery.md](references/recovery.md) before selecting any Analysis Run.
5. For a readiness check or materialization after finalization, read [approval.md](references/approval.md) before asking for or acting on approval.

Require the Selected Backup Root to remain quiescent throughout the workflow. Refuse active-folder monitoring, generic cleanup, semantic categorization, deletion, pruning, overwrite, and unapproved in-place source changes. Explain that historical backups are the primary use case, while other stable collections use the same safety model.

## Command boundary

- Run commands from the repository with `uv run filesystem-organizer ...`.
- If the locked environment is unavailable, run `uv sync --locked`, then retry.
- Use CLI JSON and Markdown output as evidence. Never reproduce hashing, relationship, Canonical Copy, planning, Structural Union, or materialization logic.
- Never edit `analysis.sqlite3` or other Run Workspace artifacts directly.
- Keep the Run Output Root outside the Selected Backup Root. Default it to the resolved `./output/` and disclose it before scanning unless the user names another root.
- Treat every Skipped Entry Finding as a scope checkpoint. Planning continues only after the user accepts exclusion of the affected entries; that answer authorizes planning scope only.
- Treat the deterministic Canonical Copy policy as part of plan construction, not as a user approval gate. State the policy and summarize any existing overrides; inspect or change an individual choice with draft `plan-override` only when the user raises a specific concern.
- Finalize only after the user has reviewed the compact persisted result structure and confirmed that its broad organization is right. Semantic placements, exclusions, Content-Empty Results, in-place destruction, and materialization retain their own explicit gates.
- Invoke `materialize` with `--yes` only after the exact approval protocol in [approval.md](references/approval.md). Do not offer cleanup, deletion, pruning, overwrite, or source changes after materialization.
