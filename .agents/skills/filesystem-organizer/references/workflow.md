# Analysis and planning workflow

## Choose the branch

- **Scan-only:** the user asks to inventory, analyze, find exact duplicates, or produce evidence without reorganizing.
- **Reorganization:** the user asks to consolidate or create a separate organized result from a stable filesystem collection.

Confirm that the Selected Backup Root will remain quiescent for the complete workflow. Resolve the Run Output Root (`./output/` by default), verify it is outside the Selected Backup Root, and tell the user both resolved paths before scanning.

## Scan-only

1. Run `uv run filesystem-organizer scan SELECTED_ROOT --output-root OUTPUT_ROOT`.
2. Record the returned `analysis_run`, `run_id`, and `snapshot_id`.
3. Run `uv run filesystem-organizer report ANALYSIS_RUN`.
4. Summarize inventory and exact/structural evidence. List every Skipped Entry Finding with its reason.
5. Stop. A scan-only request does not authorize `plan`, finalization, or materialization.

## Reorganization through finalization

1. Run the scan and report steps above.
2. If Skipped Entry Findings exist, explain that they remain excluded and ask whether planning may continue on the processed evidence. Stop until the user answers. Treat the answer only as a scope decision.
3. Run `uv run filesystem-organizer plan ANALYSIS_RUN` and record its exact `plan_id`.
4. Run `uv run filesystem-organizer plan-report ANALYSIS_RUN --plan-id PLAN_ID`.
5. Summarize the destination, operation counts, Structural Unions, conflict projections, explicit directories, and the deterministic Canonical Copy policy. Canonical Copy selection retains one occurrence of already proven identical bytes: newest modification timestamp wins, with deterministic relative-path fallback for a tie or unavailable timestamp. Do not enumerate every choice or ask the user to approve this policy.
6. Render a compact persisted structure with `uv run filesystem-organizer plan-report ANALYSIS_RUN --plan-id PLAN_ID --section structure --depth 4`. Treat direct destination directories as level 1, render four directory levels without a synthetic root label, and summarize file counts rather than enumerating files. Show the paths as a readable tree and call out material structural effects and exclusions.
7. Immediately after the standard deterministic structure, ask once whether the user wants a semantically optimized proposal for a smarter, more meaningful, categorized, renamed, or further consolidated directory structure. A positive answer authorizes analysis and a proposal only: invoke `consolidation-plan-dialogue` and use its optimization-proposal branch. That branch leads with the proposal rather than another preference question. A negative answer returns to ordinary persisted-structure confirmation.
8. If the user raises a specific Canonical Copy concern while the plan is a draft, inspect the relevant Exact Duplicate evidence. Run `uv run filesystem-organizer plan-override ANALYSIS_RUN SOURCE_RELATIVE_PATH "REASON" --plan-id PLAN_ID` only for a requested alternative, then render the affected evidence and structure again. A finalized plan is immutable; a later change requires a new plan.
9. If the user requests structural changes directly, follow `consolidation-plan-dialogue` and repeat the persisted-structure review after each applied revision. Preserve the acknowledgement gates for semantic placements, exclusions, and Content-Empty Results.
10. When the user confirms the persisted result structure after accepting or declining the optimization offer, run `uv run filesystem-organizer plan-finalize ANALYSIS_RUN --plan-id PLAN_ID` without a redundant Canonical Copy or finalization prompt.
11. Render the exact plan again and verify the report says `finalized`.
12. Report the Analysis Run, exact plan ID, intended destination, and finalized evidence. Then read [approval.md](approval.md) before any readiness check, approval request, or materialization.

Keep every command bound to the explicit Analysis Run and, once created, the explicit plan ID.
