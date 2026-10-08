# Approval-bound materialization

Materialization is the final execution approval gate. A finalized plan, deterministic Canonical Copy selection, accepted Skipped Entry scope, confirmed result structure, or a successful preflight do not authorize copying.

## Current preflight

When the user asks whether an exact finalized plan is ready—or immediately before requesting approval—run:

```console
uv run filesystem-organizer materialize-preflight ANALYSIS_RUN PLAN_ID
```

Use `--destination DESTINATION` only when the user explicitly names a custom destination. Otherwise use the plan's intended destination. Report the returned Analysis Run, plan ID, Selected Backup Root, destination, operation count, Structural Union count, explicit-directory count, total bytes, free bytes, and free-space decision.

If preflight fails or `sufficient_free_space` is false, stop safely. Do not ask for approval or invoke `materialize`.

## Exact approval and execution

Immediately after a successful current preflight, ask for direct approval of the displayed exact plan ID and destination. Treat approval as valid only for that one action and only in the current uninterrupted decision sequence.

Accept only a direct, unambiguous authorization bound to those facts, including an explicit request to `materialize PLAN_ID`. Never treat “continue,” “looks good,” Skipped Entry scope consent, result-structure confirmation, plan review, or finalization as materialization authority.

Discard any earlier approval and start again with a new preflight if the destination, plan, or scope decision changes; preflight fails; or the conversation resumes.

After valid approval, invoke:

```console
uv run filesystem-organizer materialize ANALYSIS_RUN PLAN_ID --yes
```

Include `--destination DESTINATION` only when that explicitly named custom destination was the one displayed in the current preflight. The CLI retains its own finalized-plan validation, source and Structural Snapshot Revalidation, destination safety, no-overwrite behavior, and journaled resumability; do not replace or bypass those checks.

## Outcome

Report the CLI result, including its completed status and returned operation and byte facts, as verification evidence alongside the created Materialized Consolidation destination. Explain that the Selected Backup Root was left unchanged and that a repeated or resumed call uses the CLI's journaled, idempotent behavior. Do not offer agent-mode cleanup or deletion.

## Review scenarios

- Scan-only requests stop after scan evidence and report; they never reach this approval flow.
- Active-folder organization and generic cleanup are refused as out of scope.
- Skipped Entry Findings pause planning scope; their acceptance is not materialization approval.
- Deterministic Canonical Copy selection needs no user approval. A specific objection is handled only on a draft plan with `plan-override` before finalization.
- Multiple recovery candidates are listed and require explicit selection; the newest is never inferred.
- Casual assent is rejected; only exact current approval can authorize `--yes`.
- A successful preflight immediately precedes exact approval, and any stale approval is discarded.
- After materialization, report evidence and destination without offering deletion or cleanup.
