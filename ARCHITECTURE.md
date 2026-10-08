# Custom Layout Architecture

## Purpose

Add user-designed directory layouts without making the agent or materializer interpret informal instructions. A deterministic Custom Layout module compiles compact rules against an immutable Baseline Projection, persists revisioned results, and exposes the same finalized Plan Projection seam already consumed by materialization.

The existing Analysis Run remains immutable evidence; only draft-plan state changes during layout design.

## Architectural shape

```text
Analysis Run evidence
        │
        ▼
Consolidation Plan creation
        │
        ├── immutable Baseline Projection
        └── Canonical Source Selections
                  │
                  ▼
          Custom Layout module
       export / validate / apply
                  │
                  ├── Layout Revisions
                  ├── validation findings
                  └── resolved dispositions
                             │
                             ▼
                     plan finalization
                             │
                             ▼
                  Finalized Plan Projection
                             │
                             ▼
                 materialization preflight
                             │
             ┌───────────────┴───────────────┐
             ▼                               ▼
    separate destination             in-place execution
```

The external seam is the Finalized Plan Projection. Layout rules, precedence, acknowledgements, and revision history remain behind the Custom Layout module's interface.

## Modules and interfaces

### Consolidation Plan lifecycle

Keep `filesystem_organizer.consolidation_plan` as the plan lifecycle owner. It creates the Baseline Projection, manages Canonical Source Selections, delegates custom-layout work, finalizes the active revision, and refuses every mutation after finalization.

Its stable interface gains these operations:

```python
export_layout(analysis_run, plan_id, output_path) -> LayoutExport
validate_layout(analysis_run, plan_id, layout_path) -> LayoutValidation
apply_layout(
    analysis_run,
    plan_id,
    layout_path,
    acknowledgements,
) -> AppliedLayoutRevision
finalize_consolidation_plan(analysis_run, plan_id) -> FinalizedPlan
```

Canonical Copy overrides update only the selected proven source occurrence for a stable baseline entry. They do not change its baseline identity, baseline output path, Layout Rules, or Placement. Finalization snapshots the selected sources into the executable projection. This preserves REQ-13 without making source selection part of layout authoring.

### Custom Layout module

Create an internal `filesystem_organizer.consolidation_plan.custom_layout` module. Its public interface is the three layout operations above; parsing, indexing, compilation, validation, and persistence remain internal seams.

The module performs four jobs:

1. Strictly decode and encode the versioned JSON contract.
2. Compile Layout Rules and Entry Exceptions against one immutable Baseline Projection.
3. Return every error, acknowledgement-required finding, and warning in one `LayoutValidation` result.
4. Persist one new immutable Layout Revision atomically after repeating validation under a write lock.

Validation returns results and does not mutate plan state. Application never trusts a prior validation result: it recompiles under `BEGIN IMMEDIATE`, checks the Plan ID and base revision, verifies supplied acknowledgements, then commits the revision and active-revision pointer together.

### Finalized projection loader

Introduce one internal materialization-facing interface:

```python
load_finalized_execution_plan(
    analysis_run,
    plan_id,
    mode,
    destination,
) -> ExecutionPlan
```

`ExecutionPlan` contains only executable facts:

- placed files with stable entry identity, selected source occurrence, output path, size, and digest;
- explicit baseline and Layout-Owned Directories;
- excluded baseline identities and every proven source occurrence represented by them;
- acknowledged skipped-finding outcomes (`retain` or `exclude` for in-place execution);
- Structural Snapshot Revalidation roots;
- content-empty and aggregate byte/count facts;
- finalized projection fingerprint.

The materializer never reads `layout.json`, applies Layout Rules, resolves precedence, or invents exclusions. This keeps the module deep and makes the execution seam the test surface.

### Reporting

Extend the existing plan reporter rather than adding another reporting command:

```console
filesystem-organizer plan-report RUN --plan-id PLAN_ID \
  --section structure --depth 3
```

The bounded structure view is generated from the active resolved revision, not by reapplying authoring rules. It reports the revision, output tree, collapsed file/directory counts, User Exclusion roots and totals, Layout-Owned Directories, Unmanaged Retentions, and validation status. Full and pageable sections expose exact dispositions and findings.

## JSON authoring contract

Version two uses the compact contract and binds authoring to the immutable baseline fingerprint:

```json
{
  "layout_schema_version": 2,
  "plan_id": "P1",
  "base_revision": 2,
  "baseline_fingerprint": "FINGERPRINT_FROM_EXPORTED_LAYOUT",
  "unmatched": "preserve",
  "rules": [
    {
      "selector": {"subtree": "snapshot-2025/Home/Projects"},
      "action": {"place_under": "Projects"}
    },
    {
      "selector": {"subtree": "snapshot-2025/Home/.cache"},
      "action": {"exclude": true}
    }
  ],
  "entry_exceptions": [
    {
      "entry_id": "E42",
      "action": {"place_at": "Finance/Taxes/receipt.pdf"}
    }
  ],
  "directories": ["To Sort"]
}
```

Rules are evaluated against Baseline Projection paths, never against another rule's result. Resolution precedence is:

1. exact stable-identity Entry Exception;
2. deepest matching exact subtree Layout Rule;
3. fixed `preserve` fallback.

Equal-specificity contradictory instructions are errors. `place_under` replaces the selected subtree prefix and preserves descendants. `place_at` is valid only for one exact entry. The executable language has no globs, regular expressions, semantic selectors, sequential moves, default exclusion, or content transformations.

The decoder rejects unknown schema versions, unknown required fields, invalid discriminators, duplicate rule identities, absolute or non-normalized paths, parent traversal, NULs, and implementation-defined Plan-Owned Staging Namespaces. Unknown optional fields are rejected in version two so spelling mistakes cannot silently change intent.

## Stable identity and baseline fingerprint

Persist an opaque stable `entry_id` for every Baseline Projection entry at plan creation. Its derivation is an implementation detail and must not depend on the selected Canonical Copy occurrence. File identity is anchored by the proven content-identity tuple; directory identity is anchored by entry kind and immutable baseline path. IDs are unique within a Plan.

Persist a baseline fingerprint over ordered entry IDs, kinds, baseline paths, content identities, explicit-directory facts, and relevant skipped findings. Exclude the selected Canonical Copy occurrence from this fingerprint so a proven source override does not invalidate layout intent.

Every export carries `plan_id` and `base_revision`. Application rejects a different plan, finalized plan, stale revision, unsupported schema, or mismatched persisted baseline fingerprint.

## Persistence model

Bump the Consolidation Plan schema version. Existing Analysis Run evidence does not require a rescan, but an older plan must be redrafted under the new plan schema before custom-layout use.

Use normalized tables with foreign keys rather than treating JSON as executable state:

- `plan_baseline_entries`: immutable entry identity, baseline path, kind, content identity, and generated reason;
- `plan_source_selections`: current draft Canonical Source Selection keyed by stable entry identity;
- `plan_layout_revisions`: revision number, canonical authoring JSON, baseline fingerprint, timestamps, content-empty facts, and aggregate counts;
- `plan_layout_rules`: normalized rules and exact exceptions for audit/reporting;
- `plan_layout_entries`: every baseline entry's resolved `place` or `exclude` disposition and nullable output path;
- `plan_layout_directories`: explicit Layout-Owned Directories;
- `plan_layout_findings`: stable finding ID, severity, code, affected entries/paths/rules, allowed resolution categories, and details;
- `plan_layout_acknowledgements`: exact finding and outcome accepted for one revision;
- `final_plan_entries`: immutable finalized placements with selected source occurrences;
- `final_plan_exclusions`: immutable finalized excluded identities and their proven occurrences;
- `final_plan_skipped_actions`: acknowledged `retain` or `exclude` outcomes for affected Skipped Entry Findings.

The Consolidation Plan row stores the active draft revision and finalized revision. Revision zero represents the generated Baseline Projection with preserve-unmatched behavior. Successful application inserts a complete new revision and advances the active pointer in one transaction; prior revisions remain immutable. Finalization validates and snapshots only the active revision.

Do not keep a second mutable copy of the active executable paths in `plan_operations`. That would create competing truths. Compatibility queries should route through the finalized projection loader or a read-only database view.

The premature `plan-remap` function and `plan_path_remaps` table are superseded by this model and should be removed before implementation begins.

## Compilation and validation

Compilation is deterministic and exhaustive:

1. Load the immutable baseline, current revision, skipped findings, and Plan-Owned Staging Namespace rules in one stable transaction.
2. Resolve each baseline entry to one Placement or User Exclusion using the precedence rules.
3. Add explicit Layout-Owned Directories and derive only the implicit parent directories required by placements.
4. Project exact subtree rules over skipped findings to identify mode-specific Unmanaged Retention or Unverified Exclusion decisions.
5. Build a complete output-path index and collect every finding rather than failing at the first one.
6. Compute aggregate placed/excluded counts and bytes, including unknown-byte facts for unprocessable entries.
7. Return a canonical validation result and, when applying, persist the exact resolved revision.

Unconditionally blocking errors include:

- unsafe, absolute, non-normalized, or plan-owned paths;
- unknown entry identities or selectors that match no exact baseline subtree;
- duplicate output paths for distinct entries;
- file/directory ancestry conflicts;
- contradictory equal-specificity rules;
- stale Plan ID, revision, or baseline fingerprint;
- a file placed at the output root;
- technical inability to represent the resolved projection.

Acknowledgement-required findings include:

- every User Exclusion, summarized by exact roots and complete pageable evidence;
- each Skipped Entry Finding affected during in-place planning;
- a Content-Empty Result.

Warnings include materializable but surprising facts such as an extension-changing rename or a name with cross-platform portability concerns. Warnings never replace actual-destination checks.

The CLI returns stable machine-readable finding codes, affected entry IDs and paths, originating rule indexes, and allowed resolution categories. It does not select semantic resolutions. The agent groups findings, proposes a concrete revised structure, and revalidates after each accepted rule batch.

## Destination and mode validation

Separate mode-neutral projection validity from execution readiness:

- `plan-layout-validate` and `plan-layout-apply` prove internal projection validity.
- `materialize-preflight` proves current source evidence, destination shape, available space, filesystem feasibility, mode-specific skipped actions, and recoverability.

Custom output names may be invented rather than inherited from the source. Preflight therefore adds a destination-local feasibility check for case-equivalent collisions and unsupported components. It checks the actual intended destination filesystem and cleans any probe state before returning. A destination override always triggers a new current preflight.

The existing materializer retains final authority: it continues no-overwrite publication, source/content revalidation, destination refusal, journal admission, reconciliation, and final manifest verification.

## Materialization data flow

### Separate destination

1. Preflight loads the finalized Execution Plan and revalidates sources and Structural Snapshots.
2. The execution manifest records placed files/directories and finalized projection fingerprint.
3. Materialization copies only Placements and creates explicit directories.
4. User Exclusions and Skipped Entry Findings remain absent from the result and are reported; the Selected Backup Root remains unchanged.
5. Repeated execution reconciles against the same immutable manifest.

### In-place

Reuse the existing forward-resumable journal and hard-link protection protocol. Extend the execution manifest with disposition and stable entry identity; do not add rollback.

Execution phases are:

1. Revalidate the finalized plan, Structural Snapshot, every placed source, every proven occurrence of excluded content, and the current lstat evidence for affected skipped entries.
2. Durably protect every placed source and every proven occurrence scheduled for exclusion using same-filesystem hard links.
3. Publish and verify all Placements using the existing reconciliation rules.
4. Remove noncanonical proven duplicate occurrences and explicitly excluded proven occurrences only while their protection remains durable.
5. For an acknowledged Unverified Exclusion, move the filesystem entry into plan-owned staging without following or interpreting it, journal the transition, and refuse safely if the move cannot be performed.
6. Leave every acknowledged Unmanaged Retention unchanged at its original path and verify that fact for reporting.
7. Verify the complete placed output and mode-specific qualifications, durably record materialization completion, then remove all plan-owned protection. Return success only after cleanup is complete.

A power loss before terminal completion leaves sufficient staged state to resume the exact plan forward. A power loss after the completion event but before cleanup resumes cleanup idempotently. After a successful return, no recovery copy of a User Exclusion remains.

Excluding one baseline file identity removes every proven source occurrence represented by that identity during in-place execution. This preserves result-focused semantics rather than leaving noncanonical paths behind.

## Approval boundaries

Layout acknowledgement and materialization approval remain distinct authorities:

- applying a revision with exclusions requires `--acknowledge-exclusions`;
- exact skipped-finding outcomes are supplied as finding-bound acknowledgement arguments and persisted with that revision;
- a Content-Empty Result requires a distinct acknowledgement before apply/finalize;
- after finalization, current materialization preflight precedes the existing exact plan-and-destination approval;
- a Content-Empty Result is the sole two-approval exception: one approval binds the content-empty revision, and a later approval binds execution after preflight.

The skill specifies the required facts and authority but does not prescribe canned user-facing wording.

## CLI contract

Add:

```console
filesystem-organizer plan-layout-export RUN PLAN_ID --output layout.json
filesystem-organizer plan-layout-validate RUN PLAN_ID layout.json
filesystem-organizer plan-layout-apply RUN PLAN_ID layout.json \
  [--acknowledge-exclusions] \
  [--acknowledge-finding FINDING_ID=retain|exclude]... \
  [--acknowledge-content-empty]
filesystem-organizer plan-report RUN --plan-id PLAN_ID \
  --section structure --depth N
```

Export refuses to overwrite an existing file unless a future explicit overwrite option is added. Validate emits the complete structured result and performs no plan mutation. Apply repeats validation and either commits one complete revision or changes nothing.

Existing `plan-finalize`, `materialize-preflight`, and `materialize` commands remain the lifecycle and execution gates. Their explicit Plan ID requirements remain unchanged.

## Failure and retry boundaries

- JSON decode or schema error: no database write.
- Invalid or acknowledgement-incomplete layout: structured refusal, no revision.
- Stale base revision: structured refusal, no revision; export again.
- Apply interruption: SQLite transaction rolls back; active revision is unchanged.
- Finalization interruption: transaction rolls back; the plan remains a draft.
- Preflight failure: no execution admission or destination mutation.
- Materialization interruption: existing durable journal and execution manifest resume forward.
- Terminal materialization conflict or source drift: existing needs-attention latch applies; a new Analysis Run and plan are required where current policy requires them.

## Constraints and risks

- Large plans must not require the agent to read every entry. Compilation and reports should use indexed baseline paths and streaming/pageable output.
- Rule matching should use a prefix index or trie so compilation is proportional to entries plus matched rule depth, not entries multiplied by rules.
- SQLite remains the only persisted authority. Never execute directly from exported JSON.
- Exact output-path validation must use POSIX-relative plan paths consistently while destination feasibility uses the host filesystem.
- Skipped entries cannot be treated as verified content. Their acknowledged mode-specific outcomes must remain visibly qualified.
- In-place exclusions expand the destructive surface. Crash-point tests must cover protection, publication, proven exclusion, unverified staging, completion, and cleanup boundaries.
- The current Structural Union report assumes generated output roots. Custom reporting must separate immutable structural evidence from resolved custom output paths rather than rewriting evidence to look custom-derived.

## Traceability

- Baseline, identity, precedence, and revisioning: REQ-1–3, REQ-9, REQ-12–13, REQ-16, REQ-18, REQ-23–25.
- Validation and reporting: REQ-4–6, REQ-19–22, REQ-26, REQ-30–32, REQ-34, REQ-36, REQ-39, REQ-41.
- Exclusions and skipped entries: REQ-10, REQ-14, REQ-21, REQ-27–29, REQ-33, REQ-37, REQ-40, REQ-42.
- Finalization, approval, and execution: REQ-7–8, REQ-11, REQ-15, REQ-17, REQ-20, REQ-31, REQ-35, REQ-38.

## Verification strategy

Test through the public module and CLI interfaces:

- pure compiler tests for precedence, prefix replacement, exact exceptions, exclusions, directories, all conflict categories, and complete finding collection;
- lifecycle tests for stable identities, Canonical Source Selection independence, revision concurrency, atomic application, acknowledgements, and finalization immutability;
- report tests for bounded structure trees, paging, exclusions, warnings, and Unmanaged Retentions;
- black-box CLI tests for export, validate, apply, re-export, stale refusal, finalize, and both preflight modes;
- end-to-end separate-destination comparisons against the resolved projection;
- end-to-end in-place comparisons including all duplicate occurrences and User Exclusions;
- crash/restart tests at every new protection, exclusion, staging, completion, and cleanup boundary;
- source-drift, skipped-entry drift, destination-name feasibility, plan-owned namespace, content-empty, and approval-gating tests;
- unchanged Selected Backup Root assertions for every non-in-place path.
