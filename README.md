# Filesystem Organizer

[![CI](https://github.com/stuermerr/filesystem-organizer/actions/workflows/ci.yml/badge.svg)](https://github.com/stuermerr/filesystem-organizer/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue.svg)](pyproject.toml)

> A local, safety-first CLI for consolidating and reorganizing stable filesystem collections while preserving the originals by default.

Filesystem Organizer inventories one quiescent **Selected Backup Root**, proves byte-for-byte duplicates, compares compatible directory trees, and creates a reviewable **Consolidation Plan**. Historical backup collections are the primary use case, but any explicitly selected, stable filesystem collection can use the same reviewed workflow. The CLI only writes a result after the plan has been finalized and explicitly approved.

The normal workflow creates a separate **Materialized Consolidation** and leaves the source untouched. A separately acknowledged `--in-place` mode exists for constrained historical backup volumes, but is destructive and is never the default.

## Why use it?

- **Source-preserving by default.** Scanning, reporting, and planning do not modify the Selected Backup Root. Normal materialization writes to a separate destination.
- **Content-proven deduplication.** Exact Duplicate Groups require a full BLAKE3-256 identity; matching names, paths, sizes, or timestamps are not treated as proof.
- **Durable, reviewable evidence.** Every scan produces an Analysis Run containing inventory evidence, findings, plans, and recovery state.
- **Safe structural consolidation.** Directory analysis distinguishes identical, contained, union-compatible, and conflicting trees.
- **Explicit approval gates.** Plans remain drafts until reviewed and finalized. Materialization requires the exact finalized plan ID and user approval.
- **Visible uncertainty.** Symbolic links, unreadable files, changed files, and special entries become Skipped Entry Findings instead of being silently followed or discarded.
- **Custom output layouts.** A draft plan can be reorganized with deterministic subtree rules and exact-entry exceptions that are validated before application.
- **Interruption recovery.** Scan and materialization state is persisted so an interrupted operation can be inspected and, where supported, resumed safely.

## Scope

This project is for **Safe Filesystem Consolidation and Reorganization**: an explicitly selected collection that remains stable while it is analyzed, planned, and materialized. Years of copied, renamed, modified, or nested drive backups are the motivating example, not the only eligible input.

It is not a live folder organizer, a continuously running Downloads cleaner, a semantic file classifier, or an autonomous deletion tool. Active working directories must first be copied or made quiescent. The deterministic workflow operates on filesystem metadata, full content identities, and directory structure; it does not inspect file contents to infer categories. Users can still design a meaningful final directory structure through reviewed, exact Custom Layout rules.

## Current implementation

Version `0.1.0` implements the complete local lifecycle for:

- checkpointed scan and resume;
- exact duplicate detection and reviewable Canonical Copy selection;
- deterministic directory relationships, Structural Unions, and lossless conflict projection;
- persisted draft plans, overrides, Custom Layout revisions, and immutable finalization;
- bounded reports for large inventories and output trees;
- source-preserving materialization with preflight, exact approval, source revalidation, no-overwrite behavior, and interruption recovery; and
- an explicitly acknowledged in-place mode for quiescent historical backup roots.

The repository currently has no graphical interface or semantic content-analysis stage.

### Performance expectations

The safety model deliberately favors evidence and recoverability over raw copy
speed. A recorded 62,676-entry, 17.23-GB backup scan completed in about one
minute, while conflict-heavy planning took about four minutes and conservative
materialization took about 29 minutes. Results depend strongly on storage,
filesystem, file count, and conflict shape. Large collections should be tested
with enough free space and time before relying on a production run.

Custom Layout edits use indexed, incremental revisions. Small edits are tested
against 10k and 100k projections in normal CI; a scheduled benchmark exercises
100k and 1M projections. General plan construction, high-fanout structural
candidates, and materialization throughput remain optimization areas. Source
revalidation, content verification, durable publication, and crash recovery
are not relaxed for performance.

## Getting started

### Requirements

- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/)

Clone the repository and create the environment from the committed lockfile:

```console
git clone https://github.com/stuermerr/filesystem-organizer.git
cd filesystem-organizer
uv sync --locked
```

Confirm that the CLI is available:

```console
uv run filesystem-organizer --help
```

### Run a source-preserving consolidation

Choose two separate locations:

- **Selected Backup Root:** the historical backup collection to inspect.
- **Run Output Root:** storage for Analysis Runs and default materialized results. It must be outside the Selected Backup Root.

Start a scan:

```console
uv run filesystem-organizer scan /path/to/historical-backups \
  --output-root /path/to/run-output
```

The command prints JSON containing `run_id`, `snapshot_id`, and `analysis_run`. Progress is written to stderr, leaving stdout safe to parse. Use the returned `analysis_run` path as `RUN_PATH` below.

```console
# Review concise run facts, skipped entries, and conflicts.
uv run filesystem-organizer report RUN_PATH

# Create and review a draft plan.
uv run filesystem-organizer plan RUN_PATH
# Record the returned plan_id as PLAN_ID, then keep every later command bound to it.
uv run filesystem-organizer plan-report RUN_PATH --plan-id PLAN_ID

# Optionally select a different copy from an Exact Duplicate Group.
uv run filesystem-organizer plan-override RUN_PATH \
  'relative/source/path' 'Reason this copy should be retained' \
  --plan-id PLAN_ID

# Freeze the reviewed plan.
uv run filesystem-organizer plan-finalize RUN_PATH --plan-id PLAN_ID

# Validate the finalized plan, destination, source evidence, and free space.
uv run filesystem-organizer materialize-preflight RUN_PATH PLAN_ID

# Materialize after reviewing the preflight result.
uv run filesystem-organizer materialize RUN_PATH PLAN_ID
```

In an interactive terminal, `materialize` displays the plan and destination and asks you to type `yes`. In an explicitly approved non-interactive workflow, pass the exact `PLAN_ID` with `--yes`.

For detailed audit evidence, request the full reports. High-cardinality conflicts
and Skipped Entry Findings remain bounded to a 20-item sample even in full reports;
request their complete evidence through explicit pages:

```console
uv run filesystem-organizer report RUN_PATH --detail full
uv run filesystem-organizer report RUN_PATH \
  --section inventory --offset 0 --limit 100
uv run filesystem-organizer report RUN_PATH \
  --section skipped --offset 0 --limit 100
uv run filesystem-organizer report RUN_PATH \
  --section conflicts --offset 0 --limit 100
uv run filesystem-organizer plan-report RUN_PATH --plan-id PLAN_ID \
  --section conflicts --offset 0 --limit 100
uv run filesystem-organizer plan-report RUN_PATH --plan-id PLAN_ID --detail full
```

### Customize the result layout

Custom Layout is a structure-only customization layer: it can place, rename, merge, or exclude baseline entries and create empty directories, but it never edits file bytes, converts formats, or unpacks archives. Rules operate on the immutable Baseline Projection of a draft plan. Export the current revision instead of writing a layout file from scratch:

```console
uv run filesystem-organizer plan-layout-export RUN_PATH PLAN_ID \
  --output layout.json
```

For example, this rule moves the `documents` subtree beneath `Notes`, preserves unmatched entries, and creates an intentional empty directory:

```json
{
  "layout_schema_version": 2,
  "plan_id": "PLAN_ID",
  "base_revision": 0,
  "baseline_fingerprint": "FINGERPRINT_FROM_EXPORTED_LAYOUT",
  "unmatched": "preserve",
  "rules": [
    {
      "selector": {"subtree": "documents"},
      "action": {"place_under": "Notes"}
    }
  ],
  "entry_exceptions": [],
  "directories": ["To Sort"],
  "skipped_actions": []
}
```

`rules` select exact source subtrees. `place_under` replaces the selected root while preserving descendant paths; `exclude` explicitly omits the selected subtree. `entry_exceptions` use a stable `entry_id` from CLI evidence to place or exclude one entry. The deepest matching subtree rule wins, and an exact-entry exception wins over every subtree rule. Unmatched entries are always preserved.

Validate the complete result, apply it atomically, and inspect the persisted structure before finalizing:

```console
uv run filesystem-organizer plan-layout-validate RUN_PATH PLAN_ID layout.json
uv run filesystem-organizer plan-layout-apply RUN_PATH PLAN_ID layout.json
uv run filesystem-organizer plan-report RUN_PATH --plan-id PLAN_ID \
  --section structure --depth 3
```

Validation is non-mutating and returns all errors, acknowledgement requirements, and warnings together. Applying produces a new immutable revision and refuses a stale `base_revision`; re-export before the next edit. Use `--acknowledge-exclusions` only after reviewing explicit exclusions, and add `--acknowledge-content-empty` when validation reports `placed_content_entry_count: 0`. A Content-Empty Result also requires a separate approval after materialization preflight.

For in-place planning, every Skipped Entry Finding needs an explicit `skipped_actions` decision: `retain` leaves it unchanged as an Unmanaged Retention, while `exclude` authorizes its omission subject to validation and acknowledgement. Separate-destination materialization never changes the skipped source entry.

### In-place mode

`--in-place` destructively applies the finalized Plan Projection inside the Selected Backup Root. It is intended only for a quiescent historical copy when a separate destination is impractical.

The mode does not accept `--destination`, requires `--acknowledge-destructive` in addition to normal approval, and protects retained files with hard links before changing source names. Interactive approval requires typing the exact Selected Backup Root. Always run the matching preflight first:

If the Analysis Run contains Skipped Entry Findings, an applied Custom Layout must first assign each one an explicit `retain` or `exclude` action in `skipped_actions`; in-place preflight refuses an unresolved finding.

```console
uv run filesystem-organizer materialize-preflight RUN_PATH PLAN_ID --in-place
uv run filesystem-organizer materialize RUN_PATH PLAN_ID \
  --in-place --acknowledge-destructive
```

## Safety model

```text
Selected Backup Root ──scan──> Analysis Run ──plan──> Draft Plan
       unchanged                                      │
                                                     review / customize
                                                              │
                                                              ▼
                                                     Finalized Plan ──approve──> Materialized Consolidation
                                                                               (separate destination)
```

Before copying, the CLI revalidates the finalized projection, selected source evidence, structural snapshots, destination shape, and available space. A destination override must remain outside the Selected Backup Root, and a non-empty unrelated destination is refused.

Materialization is resumable after an interrupted owned publication. A completed destination is never reused automatically. It retains every unique readable file and one deterministic Canonical Copy per Exact Duplicate Group. Compatible directory trees form Structural Unions. Conflicting trees use a lossless projection that keeps a uniquely newest regular-file variant at the conventional path and preserves all other variants in deterministic conflict paths.

## CLI reference

Run `uv run filesystem-organizer COMMAND --help` for complete flags and arguments.

| Command | Purpose |
| --- | --- |
| `scan ROOT --output-root OUTPUT_ROOT` | Create a persisted Analysis Run for one backup root. |
| `report RUN_PATH` | Render bounded run facts or requested full/paged evidence. |
| `runs [OUTPUT_ROOT]` | List Analysis Runs beneath a Run Output Root. |
| `status RUN_PATH` | Inspect persisted run, plan, and recovery state. |
| `resume RUN_PATH` | Continue an interrupted scan from its checkpoint. |
| `plan RUN_PATH` | Create a draft Consolidation Plan for a completed run. |
| `plan-report RUN_PATH` | Review plan facts or page operations, structure, exclusions, findings, and conflicts. |
| `plan-override RUN_PATH SOURCE_PATH REASON` | Change the Canonical Copy selected in a draft plan. |
| `plan-layout-export RUN_PATH PLAN_ID --output FILE` | Export the current Custom Layout revision. |
| `plan-layout-validate RUN_PATH PLAN_ID FILE` | Validate a layout without changing the plan. |
| `plan-layout-apply RUN_PATH PLAN_ID FILE` | Revalidate and atomically apply a layout as a new immutable revision; exclusions and content-empty outcomes require acknowledgements. |
| `plan-layout-rebuild-active RUN_PATH PLAN_ID --verify` | Verify the rebuildable active projection; `--repair` performs an explicit atomic repair. |
| `plan-finalize RUN_PATH` | Freeze a reviewed draft plan. |
| `materialize-preflight RUN_PATH PLAN_ID` | Validate a finalized plan and destination without copying. |
| `materialize RUN_PATH PLAN_ID` | Materialize an explicitly approved finalized plan. |

Successful commands exit with status `0`, safe operational refusals use `1`, and invalid CLI usage uses `2`.

## Development

Install the locked environment and run the quality suite:

```console
uv sync --locked
uv run ruff check filesystem_organizer tests scripts
uv run mypy filesystem_organizer tests
uv run pytest
```

The same complete gate runs in GitHub Actions.

The repository uses a committed `uv.lock`. Add application dependencies with `uv add PACKAGE` and development dependencies with `uv add --dev PACKAGE`; do not use `uv pip install` for project dependencies.

### Fixture contract

[`example_source/`](example_source/) is immutable historical-backup input and [`example_output/`](example_output/) is the expected source-preserving result. The fixture covers duplicate evidence, skipped-entry reporting, and compatible directory consolidation. Tests copy and reconstruct its metadata from [`example_fixture_manifest.json`](example_fixture_manifest.json).

[`tests/test_milestone_gate.py`](tests/test_milestone_gate.py) is the black-box acceptance gate. It runs the public workflow, checks durable artifacts, compares the materialized tree byte-for-byte with the expected output, and exercises interrupted materialization recovery.

### Scalability benchmark

Record a three-run median of the complete public workflow against fresh fixture copies:

```console
uv run python scripts/benchmark_example_fixture.py \
  --label issue-N-description \
  --history benchmarks/scalability-example.json
```

Use a distinct label after each cumulative optimization. The append-only history
records the exact Git state and execution environment, source file and byte
counts, per-stage time, peak memory, I/O, workspace sizes, structural comparison
counts, report sizes, journal activity, and materialization results.

## Documentation and support

- [Custom Layout architecture](ARCHITECTURE.md)
- [Proof-Driven Content Identity](docs/adr/0004-proof-driven-content-identity.md)
- [Phase-Level Source-Preserving Materialization](docs/adr/0005-phase-level-source-preserving-materialization.md)
- [Public-fixture scalability history](benchmarks/scalability-example.json), generated by the documented benchmark command above
- [GitHub Issues](https://github.com/stuermerr/filesystem-organizer/issues) for questions, bugs, and feature requests
- [Security policy](SECURITY.md) for confidential vulnerability reporting

## Maintainers and contributing

The project is maintained by [@stuermerr](https://github.com/stuermerr) and its contributors.
Open an issue before a substantial change, and submit a focused pull request with tests.
The project is licensed under the [MIT License](LICENSE).

## Agent-assisted workflows

Coding agents are a supported operator and design interface for this project. The repository includes:

- [`filesystem-organizer`](.agents/skills/filesystem-organizer/SKILL.md), which governs scan-only analysis, planning, recovery, safety checks, and approval-gated materialization; and
- [`consolidation-plan-dialogue`](.agents/skills/consolidation-plan-dialogue/SKILL.md), which helps a user turn a broad desired organization into a concrete, materializable Custom Layout.

In a compatible coding-agent environment, invoke `$filesystem-organizer` with a Selected Backup Root. Add `$consolidation-plan-dialogue` when you want help designing the final target structure. The agent should work top-down from the desired output directories, show compact current and recommended trees, translate accepted choices into exact subtree rules or stable-entry exceptions, validate after every coherent edit, and display the persisted structure after application. The user owns every semantic placement and exclusion decision.

The CLI remains the execution and safety boundary. An agent must use public commands, keep the exact Analysis Run and plan ID explicit, and treat CLI JSON and Markdown as evidence. It must not edit `analysis.sqlite3`, hand-author a finalized projection, duplicate hashing or planning logic, infer recovery candidates, or bypass acknowledgement and materialization gates. A successful scan, accepted layout, or finalized plan is not approval to materialize.

For an AI handoff, have the agent read both skills before running the CLI. Existing work should be recovered with `runs` and `status`; if more than one run or plan is available, the user must select the intended one.

Scanning, hashing, planning, and copying are local CLI operations. Agent use can expose filesystem metadata—including paths, filenames, timestamps, sizes, identities, structural evidence, Canonical Copy reasons, and Skipped Entry Findings—to the selected agent provider's model context. It does not provide a separate local-only agent mode; use the CLI directly when that metadata handling is not appropriate.
