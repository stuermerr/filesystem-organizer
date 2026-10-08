# Recovery through public state

Recover from persisted CLI state, not conversational memory.

1. Resolve and disclose the Run Output Root.
2. Run `uv run filesystem-organizer runs RUN_OUTPUT_ROOT`.
3. If no Analysis Run matches, report that fact and ask whether to start a new scan.
4. If multiple runs could match—or multiple runs are incomplete—list their Analysis Run paths, IDs, Selected Backup Roots, statuses, and timestamps. Require the user to select one; never choose the newest implicitly.
5. Run `uv run filesystem-organizer status ANALYSIS_RUN` for the explicitly selected path.
6. Stop safely on missing, malformed, unsupported, or ambiguous state.
7. If the selected run is incomplete and `recovery_available` is true, run `uv run filesystem-organizer resume ANALYSIS_RUN`, then inspect `status` and render `report` again.
8. For a complete run, continue from its reported plan lifecycle: create a plan when none exists, review a draft, or review a finalized plan. Keep every action bound to an explicit plan ID.

For a finalized plan, read [approval.md](approval.md) before proceeding. A resumed conversation invalidates any earlier materialization approval, even if the plan has not changed.
