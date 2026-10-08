---
name: consolidation-plan-dialogue
description: Co-design a materializable Custom Layout for safe consolidation or reorganization of a stable filesystem collection. Use when a user wants to shape, review, visualize, or decide the output directory structure, or asks for a smarter, optimized, meaningful, categorized, renamed, or further consolidated result. Exclude active-folder organization, generic cleanup, semantic file classification, and arbitrary file-to-taxonomy moves.
---

# Custom Layout Planning Dialogue

Use this skill with `filesystem-organizer`. That skill owns scope, privacy disclosure, scans, recovery, and approval-bound materialization. This skill owns the user-led Custom Layout conversation. The public CLI and its persisted plan are the only execution authority.

## Scope

Plan a separate-destination or in-place consolidation of a quiescent Selected Backup Root. Start from the desired output directories, never from a file taxonomy. Keep file-level discussion for an exception, evidence, conflict, or skipped entry the user raises.

The agent may recommend a valid structural choice, but the user approves every semantic choice that changes a placement or excludes an entry. Never invent a transformation, modify source files, edit run-workspace data, or write a finalized projection.

## Establish the result

1. Follow the applicable `filesystem-organizer` workflow through a draft plan. Keep an explicit Analysis Run and plan ID; recovery always requires the user's selected run and plan.
2. Ask for the desired high-level result only when it is not already stated and the optimization-proposal branch below does not apply. Work top-down: purpose, root directories, then two or three levels. A partial preference is enough to begin.
3. Use already-authorized plan evidence selectively. Respect Provider Approval before optional content analysis. Summarize broad evidence with CLI pages and reports; do not narrate every entry.
4. Show a compact **Current Persisted Structure**. For the initial view, run:

   ```console
   uv run filesystem-organizer plan-report ANALYSIS_RUN --plan-id PLAN_ID --section structure --depth 3
   ```

   Render its persisted paths as a readable tree with direct destination directories at level 1 and no synthetic root label. Keep the initial view to three directory levels and summarize file counts; inspect a deeper branch or page operation evidence only when the user needs it. Identify material structural effects, Layout-Owned Directories, conflicts, skipped entries, and exclusions alongside the tree.
5. Compare the user's preference with that result. When a change is useful, show a separate **Recommended Result Structure** as a two-to-three-level tree derived from supported rules and authorized evidence. It is a proposal, never a persisted result. Explain its affected structure and label it as a recommendation until the user approves it.

## Optimization-proposal branch

Use this branch when the user accepts the `filesystem-organizer` optimization offer or directly asks for a smarter, optimized, meaningful, categorized, renamed, or further consolidated structure.

1. Lead with evidence gathering, not a preference question. Run `uv run filesystem-organizer plan-report ANALYSIS_RUN --plan-id PLAN_ID --section structure --depth 10` against the current persisted draft. Page or filter this public CLI output when it is large, while examining every major destination branch and the patterns that materially affect the recommendation.
2. Inspect operation evidence selectively with `--section operations --offset N --limit N` when directory names alone leave an important placement ambiguous. Filename evidence supports directory-level recommendations; it does not authorize content inspection or arbitrary file-by-file taxonomy moves.
3. Identify actionable structural patterns: redundant path prefixes, device or backup provenance wrappers, related subjects split across roots, dated snapshots, empty retained directories, conflict projections, and project-internal dependency, cache, build, or environment trees. Preserve cohesive project internals unless the user explicitly chooses an evidenced exception.
4. Make the first substantive response a concrete **Recommended Result Structure**, normally a readable tree with two to four levels. Include concrete source-to-destination mappings for proposed directory rules, explain meaningful consolidation and renaming decisions, distinguish provenance-preserving snapshots from true merges, and label optional exclusions separately with their impact.
5. Recommend one coherent default rather than returning only a menu. Ask for approval of its semantic placements and any separately stated exclusions. Do not export or apply layout changes until the user approves those choices.

## Revise one coherent decision at a time

For each user-approved rule or coherent batch:

1. Export the current draft to a new working file. Never overwrite a draft export or assume a revision is current:

   ```console
   uv run filesystem-organizer plan-layout-export ANALYSIS_RUN PLAN_ID --output LAYOUT_JSON
   ```

2. Edit only the exported Custom Layout authoring contract. Preserve `layout_schema_version`, `plan_id`, `base_revision`, and `unmatched: "preserve"`. The supported authoring choices are:

   - `rules`: `{"selector":{"subtree":"SOURCE"},"action":{"place_under":"DESTINATION"}}` or `{"selector":{"subtree":"SOURCE"},"action":{"exclude":true}}`.
   - `entry_exceptions`: a public, stable `entry_id` with `place_at` or `exclude`, when such an ID is supplied by the CLI evidence. Do not derive IDs from private workspace state.
   - `directories`: safe relative paths for intentional Layout-Owned Directories.

   Use rules for directory structure. Use an entry exception only for a genuine individual-entry exception. Do not add fields, change the unmatched policy, or hand-author a finalized projection.
3. Validate immediately:

   ```console
   uv run filesystem-organizer plan-layout-validate ANALYSIS_RUN PLAN_ID LAYOUT_JSON
   ```

   Treat the returned JSON as authoritative. A valid result with `placed_content_entry_count: 0` is a **Content-Empty Result**. Present every returned finding, grouped by shared cause or decision, and distinguish deterministic errors, acknowledgement-required findings, warnings, agent recommendations, and user-approved resolutions. Give a recommended resolution for each group, but apply only the user-approved ones.
4. Stop on errors. If the layout is stale, re-export the current revision, reconcile the user's accepted choices, then validate again. Never overwrite a newer revision.
5. For exclusions, explain the CLI facts and likely result impact. Challenge a surprising preservation-affecting exclusion once; a confirmed, valid user rule is authoritative. Summarize broad exclusions instead of enumerating them. For in-place runs, explain skipped-entry retention or exclusion from the CLI facts without promising recovery.
6. Before applying a Content-Empty Result, obtain the first distinct explicit acknowledgement that the validated result has zero content entries. It must occur before apply or finalization, whether or not the layout has exclusions. Before applying any other valid layout with exclusions, obtain explicit acknowledgement of those exclusions. Then apply the exact validated file:

   ```console
   uv run filesystem-organizer plan-layout-apply --acknowledge-exclusions ANALYSIS_RUN PLAN_ID LAYOUT_JSON
   ```

   Omit `--acknowledge-exclusions` when there are no exclusions. A user may decline a recommendation or acknowledgement; leave it unapplied and continue planning.
7. After every successful apply, render the persisted structure—not the working JSON—and ask whether it is the exact desired result:

   ```console
   uv run filesystem-organizer plan-report ANALYSIS_RUN --plan-id PLAN_ID --section structure --depth DEPTH
   ```

   Continue the loop when it is not exact.

## Finalize and materialize

When the user confirms that the persisted structure is the exact desired match, finalize without a redundant finalize prompt. For a Content-Empty Result, proceed only when its first explicit acknowledgement was already obtained before apply; otherwise return to that gate. For a non-empty result, no separate finalization approval is needed:

```console
uv run filesystem-organizer plan-finalize ANALYSIS_RUN --plan-id PLAN_ID
uv run filesystem-organizer plan-report ANALYSIS_RUN --plan-id PLAN_ID --section structure --depth DEPTH
```

Verify the final report status, then follow `filesystem-organizer`'s approval reference: run a current mode-specific `materialize-preflight`, stop on any blocking result, and ask one explicit materialization question bound to the displayed plan ID and destination. On that approval, materialize immediately with the matching destination mode and no second prompt.

A Content-Empty Result has two gates: its first acknowledgement occurs before apply or finalization; after finalization, run the current preflight and obtain a second, separate, explicit materialization approval. Never reuse the first acknowledgement as materialization approval.

## Completion evidence

Report the exact Analysis Run, plan ID, applied layout revision, persisted structure, exclusions, finalization status, preflight facts, and materialization result where applicable. When validation or preflight remains blocking, stop with the unresolved CLI evidence and available resolution categories.
