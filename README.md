# Filesystem Organizer

[![CI](https://github.com/stuermerr/filesystem-organizer/actions/workflows/ci.yml/badge.svg)](https://github.com/stuermerr/filesystem-organizer/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue.svg)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A local, safety-first CLI for consolidating historical backups and other stable
filesystem collections without changing the source by default.

Filesystem Organizer inventories one quiescent **Selected Backup Root**, proves
byte-identical files, compares compatible directory trees, and builds a
reviewable **Consolidation Plan**. After review, it creates a separate
**Materialized Consolidation** with atomic publication and no-overwrite
protection.

> [!IMPORTANT]
> The Selected Backup Root must remain unchanged from scan through
> materialization. This is not a live-folder organizer, synchronization tool,
> semantic file classifier, or automatic deletion utility.

## Why use it?

- **Preserves the source by default.** Scanning, planning, and normal
  materialization never modify the Selected Backup Root.
- **Proves duplicates by content.** Exact Duplicate Groups use full BLAKE3-256
  identities; names, paths, sizes, and timestamps are not treated as proof.
- **Keeps evidence reviewable.** Each Analysis Run persists inventory,
  structural findings, plans, layout revisions, and execution state.
- **Consolidates directory structure safely.** Identical, contained,
  union-compatible, and conflicting directory trees receive distinct,
  deterministic treatment.
- **Makes uncertainty visible.** Symbolic links, unreadable files, changed
  files, and special entries become Skipped Entry Findings instead of being
  silently followed or discarded.
- **Supports deliberate layouts.** Validated rules can place, rename, merge,
  or exclude exact plan entries without changing file bytes.
- **Fails closed.** Source drift, stale revisions, unsafe destinations,
  insufficient space, and ambiguous recovery state stop execution.

## Status and scope

The repository targets version `0.1.0`. Linux is the only supported and
verified platform for this release.

The implemented local workflow includes checkpointed scanning, exact duplicate
detection, structural analysis, draft and finalized plans, Custom Layout
revisions, source-preserving materialization, and interruption recovery. An
explicitly acknowledged in-place mode is also available for constrained
historical backup volumes, but it is destructive and is never the default.

There is currently no graphical interface or semantic content-analysis stage.

## Getting started

### Requirements

- Linux
- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/)
- Enough free space for a separate consolidated result

Clone the repository and create the locked environment:

```console
git clone https://github.com/stuermerr/filesystem-organizer.git
cd filesystem-organizer
uv sync --locked
uv run filesystem-organizer --help
```

### Run a source-preserving consolidation

Choose two separate locations:

- **Selected Backup Root:** the stable collection to inspect.
- **Run Output Root:** storage for Analysis Runs and materialized results. It
  must be outside the Selected Backup Root.

Scan the source:

```console
uv run filesystem-organizer scan /path/to/historical-backups \
  --output-root /path/to/run-output
```

The command writes progress to stderr and prints JSON to stdout. Record the
returned `analysis_run` path as `RUN_PATH`.

Review the evidence, create a plan, and inspect the proposed result:

```console
uv run filesystem-organizer report RUN_PATH
uv run filesystem-organizer plan RUN_PATH

# Record the returned plan_id as PLAN_ID.
uv run filesystem-organizer plan-report RUN_PATH --plan-id PLAN_ID
uv run filesystem-organizer plan-report RUN_PATH --plan-id PLAN_ID \
  --section structure --depth 4
```

The plan report identifies the active layout revision. Materialize that exact
revision after reviewing the destination and proposed structure:

```console
uv run filesystem-organizer materialize RUN_PATH PLAN_ID --revision REVISION
```

In a terminal, the CLI displays the current preflight facts and asks for a
standalone confirmation. For explicitly approved non-interactive execution,
bind approval to the same revision:

```console
uv run filesystem-organizer materialize RUN_PATH PLAN_ID \
  --revision REVISION --yes
```

Normal materialization finalizes the specified draft revision, repeats its
lightweight safety checks under lock, stages the complete result, and publishes
it atomically only if the final destination does not already exist. The source
remains unchanged.

### Review detailed evidence

Summary reports are bounded for large collections. Request a full report or
page a high-cardinality section when needed:

```console
uv run filesystem-organizer report RUN_PATH --detail full
uv run filesystem-organizer report RUN_PATH \
  --section skipped --offset 0 --limit 100
uv run filesystem-organizer report RUN_PATH \
  --section conflicts --offset 0 --limit 100
uv run filesystem-organizer plan-report RUN_PATH --plan-id PLAN_ID \
  --section operations --offset 0 --limit 100
```

## Customize the output layout

Custom Layout rules operate on a plan's immutable Baseline Projection. They can
place or exclude a subtree, override one exact entry, and create intentional
empty directories. They cannot edit file contents, convert formats, or unpack
archives.

Export the current layout before editing it:

```console
uv run filesystem-organizer plan-layout-export RUN_PATH PLAN_ID \
  --output layout.json
```

A minimal exported layout has this shape:

```json
{
  "layout_schema_version": 2,
  "plan_id": "PLAN_ID",
  "base_revision": 0,
  "baseline_fingerprint": "FINGERPRINT_FROM_EXPORT",
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

Validate the entire result, apply it as a new immutable revision, and inspect
the persisted structure:

```console
uv run filesystem-organizer plan-layout-validate RUN_PATH PLAN_ID layout.json
uv run filesystem-organizer plan-layout-apply RUN_PATH PLAN_ID layout.json
uv run filesystem-organizer plan-report RUN_PATH --plan-id PLAN_ID \
  --section structure --depth 4
```

An apply refuses a stale `base_revision`. Explicit exclusions require
`--acknowledge-exclusions`; a result that places no baseline files also requires
`--acknowledge-content-empty`. Re-export before making another edit.

## Expert and in-place workflows

`plan-finalize` and `materialize-preflight` are available for automation and
advanced review:

```console
uv run filesystem-organizer plan-finalize RUN_PATH --plan-id PLAN_ID
uv run filesystem-organizer materialize-preflight RUN_PATH PLAN_ID
uv run filesystem-organizer materialize RUN_PATH PLAN_ID --yes
```

Preflight is point-in-time evidence, not durable authorization. Materialization
always repeats its internal checks.

### Destructive in-place mode

In-place execution applies a finalized Plan Projection inside the Selected
Backup Root. Use it only on a quiescent historical copy when a separate
destination is impractical.

```console
uv run filesystem-organizer plan-finalize RUN_PATH --plan-id PLAN_ID
uv run filesystem-organizer materialize-preflight RUN_PATH PLAN_ID --in-place
uv run filesystem-organizer materialize RUN_PATH PLAN_ID \
  --in-place --acknowledge-destructive
```

The mode does not accept `--destination`. Interactive approval requires typing
the exact Selected Backup Root; headless execution additionally requires
`--yes`. If Skipped Entry Findings exist, the applied Custom Layout must first
assign each one an explicit `retain` or `exclude` action.

## Safety model

```text
Selected Backup Root ──scan──> Analysis Run ──plan──> Draft Plan
       unchanged                                      │
                                                     review / customize
                                                              │
                                                              ▼
                                                   exact revision approval
                                                              │
                                                              ▼
                                               Materialized Consolidation
                                                (separate destination)
```

Before normal materialization, the CLI checks the plan and layout revision,
source metadata, destination shape, filesystem capabilities, locks, and free
space. It then:

1. creates a plan-owned partial tree beside the destination;
2. uses native cloning when supported or a verified streaming copy fallback;
3. makes the completed staging tree durable;
4. publishes it with an atomic no-replace operation; and
5. records a durable Materialization Attempt.

An incomplete tree is never exposed as the final destination. A repeated call
for the same completed plan is idempotent, while foreign, damaged, or ambiguous
staging state is refused for manual review.

The tool preserves every unique readable file and one deterministic Canonical
Copy from each Exact Duplicate Group. Compatible directory trees form
Structural Unions. Conflicts use a lossless projection that retains every
variant in deterministic paths.

## CLI overview

Run `uv run filesystem-organizer COMMAND --help` for complete arguments.

| Command | Purpose |
| --- | --- |
| `scan` | Create a persisted Analysis Run. |
| `resume` | Continue an interrupted scan from its checkpoint. |
| `runs` | Discover Analysis Runs below a Run Output Root. |
| `status` | Inspect persisted run, plan, and recovery state. |
| `report` | Review run facts and paged evidence. |
| `plan` | Create a draft Consolidation Plan. |
| `plan-report` | Review plan facts, structure, exclusions, and conflicts. |
| `plan-override` | Select another proven-identical Canonical Copy. |
| `plan-layout-export` | Export the current Custom Layout revision. |
| `plan-layout-validate` | Validate a layout without changing the plan. |
| `plan-layout-apply` | Apply a validated layout as a new revision. |
| `plan-layout-rebuild-active` | Verify or explicitly repair the active projection. |
| `plan-finalize` | Freeze a draft plan for an expert workflow. |
| `materialize-preflight` | Check a finalized plan without copying. |
| `materialize` | Finalize an exact draft revision when needed and create the approved result. |

Successful commands exit with status `0`, safe operational refusals use `1`,
and invalid CLI usage uses `2`.

## Development

Install the locked environment and run the same quality gate used by CI:

```console
uv sync --locked
uv run ruff check filesystem_organizer tests scripts
uv run mypy filesystem_organizer tests
uv run pytest
```

Use `uv add PACKAGE` for application dependencies and `uv add --dev PACKAGE`
for development dependencies. Do not use `uv pip install` to modify project
dependencies.

The end-to-end fixture contract is:

- [`example_source/`](example_source/) — immutable historical-backup input;
- [`example_output/`](example_output/) — expected source-preserving result; and
- [`tests/test_milestone_gate.py`](tests/test_milestone_gate.py) — the public
  lifecycle and recovery acceptance gate.

Before making a substantial change, open an issue first, keep the change
focused, and include tests for observable behavior changes.

## Documentation and support

- [Architecture](ARCHITECTURE.md)
- [Proof-driven content identity](docs/adr/0004-proof-driven-content-identity.md)
- [Phase-level source-preserving materialization](docs/adr/0005-phase-level-source-preserving-materialization.md)
- [Security policy](SECURITY.md)
- [GitHub Issues](https://github.com/stuermerr/filesystem-organizer/issues)
  for questions, bugs, and feature requests

Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
Do not post sensitive paths, filenames, credentials, or filesystem metadata in
a public issue.

## Maintainers and contributing

Filesystem Organizer is maintained by
[@stuermerr](https://github.com/stuermerr) and its contributors. Contributions
are welcome; open an issue before a substantial change and submit a focused
pull request with appropriate tests.

The project is available under the [MIT License](LICENSE).
