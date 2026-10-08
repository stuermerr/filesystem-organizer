from __future__ import annotations

import sqlite3
from bisect import bisect_left
from collections import defaultdict
from pathlib import Path

from ..content_identity import select_identity_rows
from ..exact_duplicate_groups import (
    READ_OUTCOME_SUCCESSFUL,
    derive_exact_duplicate_groups,
)
from .conflict_projection import component_has_conflict, project_lossless_conflicts
from .models import (
    ConflictSourceMapping,
    ConsolidationPlanError,
    LosslessConflictProjection,
    PlanOperation,
    PlanOutputEntry,
    StructuralUnion,
)
from .paths import is_within


def _create_plan_schema(connection: sqlite3.Connection) -> None:
    """Add the Consolidation Plan tables to a pre-existing Analysis Run database.

    Uses ``CREATE TABLE IF NOT EXISTS`` because these tables did not exist in
    Analysis Run databases created before this feature, and a plan may be
    drafted against such an already-complete run.
    """
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS consolidation_plans (
          plan_id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL,
          snapshot_id TEXT NOT NULL,
          schema_version INTEGER NOT NULL,
          intended_destination TEXT NOT NULL,
          created_at TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'draft',
          finalized_at TEXT,
          UNIQUE (plan_id, run_id),
          FOREIGN KEY (run_id) REFERENCES analysis_runs(run_id)
        );

        CREATE TABLE IF NOT EXISTS plan_operations (
          plan_id TEXT NOT NULL,
          operation_index INTEGER NOT NULL,
          source_relative_path TEXT NOT NULL,
          output_relative_path TEXT NOT NULL,
          expected_byte_size INTEGER NOT NULL,
          algorithm TEXT,
          algorithm_version INTEGER,
          digest TEXT,
          canonical_reason TEXT,
          modified_ns INTEGER,
          evidence_kind TEXT NOT NULL CHECK (evidence_kind IN (
            'content-identity', 'metadata-observation')),
          PRIMARY KEY (plan_id, operation_index),
          CHECK (
            (evidence_kind = 'content-identity' AND algorithm IS NOT NULL
              AND algorithm_version IS NOT NULL AND digest IS NOT NULL
              AND modified_ns IS NULL)
            OR (evidence_kind = 'metadata-observation' AND algorithm IS NULL
              AND algorithm_version IS NULL AND digest IS NULL
              AND modified_ns IS NOT NULL)
          ),
          FOREIGN KEY (plan_id) REFERENCES consolidation_plans(plan_id)
        );

        CREATE TABLE IF NOT EXISTS plan_overrides (
          plan_id TEXT NOT NULL,
          override_index INTEGER NOT NULL,
          algorithm TEXT NOT NULL,
          algorithm_version INTEGER NOT NULL,
          byte_size INTEGER NOT NULL,
          digest TEXT NOT NULL,
          selected_relative_path TEXT NOT NULL,
          prior_relative_path TEXT NOT NULL,
          reason TEXT NOT NULL,
          created_at TEXT NOT NULL,
          PRIMARY KEY (plan_id, override_index),
          FOREIGN KEY (plan_id) REFERENCES consolidation_plans(plan_id)
        );

        CREATE TABLE IF NOT EXISTS plan_output_entries (
          plan_id TEXT NOT NULL,
          entry_index INTEGER NOT NULL,
          entry_kind TEXT NOT NULL CHECK (entry_kind IN ('file', 'directory')),
          output_relative_path TEXT NOT NULL,
          source_relative_path TEXT,
          expected_byte_size INTEGER,
          algorithm TEXT,
          algorithm_version INTEGER,
          digest TEXT,
          reason TEXT NOT NULL,
          modified_ns INTEGER,
          evidence_kind TEXT NOT NULL CHECK (evidence_kind IN (
            'content-identity', 'metadata-observation', 'layout-owned-directory')),
          PRIMARY KEY (plan_id, entry_index),
          UNIQUE (plan_id, output_relative_path),
          CHECK (
            (entry_kind = 'file' AND source_relative_path IS NOT NULL
             AND expected_byte_size IS NOT NULL
             AND ((evidence_kind = 'content-identity' AND algorithm IS NOT NULL
               AND algorithm_version IS NOT NULL AND digest IS NOT NULL
               AND modified_ns IS NULL)
             OR (evidence_kind = 'metadata-observation' AND algorithm IS NULL
               AND algorithm_version IS NULL AND digest IS NULL
               AND modified_ns IS NOT NULL)))
            OR (entry_kind = 'directory' AND source_relative_path IS NULL
                AND expected_byte_size IS NULL AND algorithm IS NULL
                AND algorithm_version IS NULL AND digest IS NULL
                AND modified_ns IS NULL AND evidence_kind = 'layout-owned-directory')
          ),
          FOREIGN KEY (plan_id) REFERENCES consolidation_plans(plan_id)
        );
        CREATE TABLE IF NOT EXISTS plan_structural_roots (
          plan_id TEXT NOT NULL, root_relative_path TEXT NOT NULL,
          PRIMARY KEY (plan_id, root_relative_path),
          FOREIGN KEY (plan_id) REFERENCES consolidation_plans(plan_id)
        );
        CREATE TABLE IF NOT EXISTS plan_structural_unions (
          plan_id TEXT NOT NULL, union_index INTEGER NOT NULL,
          PRIMARY KEY (plan_id, union_index),
          FOREIGN KEY (plan_id) REFERENCES consolidation_plans(plan_id)
        );
        CREATE TABLE IF NOT EXISTS plan_projection_unions (
          plan_id TEXT NOT NULL, union_index INTEGER NOT NULL,
          canonical_root TEXT NOT NULL, classification TEXT NOT NULL,
          canonical_reason TEXT NOT NULL, qualifications TEXT NOT NULL,
          unverified_counterparts TEXT NOT NULL,
          PRIMARY KEY (plan_id, union_index),
          FOREIGN KEY (plan_id) REFERENCES consolidation_plans(plan_id)
        );
        CREATE TABLE IF NOT EXISTS plan_projection_union_roots (
          plan_id TEXT NOT NULL, union_index INTEGER NOT NULL,
          root_relative_path TEXT NOT NULL,
          PRIMARY KEY (plan_id, union_index, root_relative_path),
          FOREIGN KEY (plan_id, union_index)
            REFERENCES plan_projection_unions(plan_id, union_index)
        );
        CREATE TABLE IF NOT EXISTS plan_projection_omitted_entries (
          plan_id TEXT NOT NULL, union_index INTEGER NOT NULL,
          relative_path TEXT NOT NULL, reason TEXT NOT NULL,
          PRIMARY KEY (plan_id, union_index, relative_path),
          FOREIGN KEY (plan_id, union_index)
            REFERENCES plan_projection_unions(plan_id, union_index)
        );
        CREATE TABLE IF NOT EXISTS plan_conflict_projections (
          plan_id TEXT NOT NULL,
          group_index INTEGER NOT NULL,
          conflict_index INTEGER NOT NULL,
          source_relative_path TEXT NOT NULL,
          output_relative_path TEXT NOT NULL,
          entry_kind TEXT NOT NULL CHECK (entry_kind IN ('file', 'directory')),
          disposition TEXT NOT NULL
            CHECK (disposition IN ('primary', 'variant', 'collapsed')),
          reason TEXT NOT NULL,
          PRIMARY KEY (plan_id, group_index, conflict_index),
          UNIQUE (plan_id, source_relative_path, output_relative_path),
          FOREIGN KEY (plan_id) REFERENCES consolidation_plans(plan_id)
        );
        CREATE TABLE IF NOT EXISTS plan_projection_conflict_groups (
          plan_id TEXT NOT NULL,
          group_index INTEGER NOT NULL,
          canonical_root TEXT NOT NULL,
          canonical_reason TEXT NOT NULL,
          PRIMARY KEY (plan_id, group_index),
          FOREIGN KEY (plan_id) REFERENCES consolidation_plans(plan_id)
        );
        CREATE TABLE IF NOT EXISTS plan_projection_conflict_group_roots (
          plan_id TEXT NOT NULL,
          group_index INTEGER NOT NULL,
          root_relative_path TEXT NOT NULL,
          PRIMARY KEY (plan_id, group_index, root_relative_path),
          FOREIGN KEY (plan_id, group_index)
            REFERENCES plan_projection_conflict_groups(plan_id, group_index)
        );
        CREATE TABLE IF NOT EXISTS plan_projection_conflict_omitted_entries (
          plan_id TEXT NOT NULL,
          group_index INTEGER NOT NULL,
          relative_path TEXT NOT NULL,
          reason TEXT NOT NULL,
          PRIMARY KEY (plan_id, group_index, relative_path),
          FOREIGN KEY (plan_id, group_index)
            REFERENCES plan_projection_conflict_groups(plan_id, group_index)
        );
        """
    )


def persist_plan_projection(
    connection: sqlite3.Connection,
    plan_id: str,
    run_id: str,
    structural_unions: list[StructuralUnion],
    conflict_groups: list[LosslessConflictProjection],
) -> None:
    """Persist the selected Structural Unions as the plan's immutable meaning."""
    skipped_reasons = {
        str(row["relative_path"]): str(row["reason"])
        for row in connection.execute(
            "SELECT relative_path, reason FROM skipped_entry_findings WHERE run_id = ?",
            (run_id,),
        )
    }
    unprocessable = connection.execute(
        "SELECT relative_path, entry_kind, read_outcome FROM inventory_entries "
        "WHERE run_id = ? AND read_outcome != ? ORDER BY relative_path",
        (run_id, READ_OUTCOME_SUCCESSFUL),
    ).fetchall()
    relationships = {
        (str(row["left_root"]), str(row["right_root"])): (
            str(row["qualifications"]),
            str(row["unverified_counterparts"]),
        )
        for row in connection.execute(
            "SELECT left_root, right_root, qualifications, unverified_counterparts "
            "FROM directory_relationships WHERE run_id = ?",
            (run_id,),
        )
    }
    for index, union in enumerate(structural_unions):
        qualifications, unverified = (
            relationships.get((union.roots[0], union.roots[1]), ("", ""))
            if len(union.roots) == 2
            else ("", "")
        )
        connection.execute(
            "INSERT INTO plan_projection_unions VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id,
                index,
                union.canonical_root,
                union.classification,
                union.canonical_reason,
                qualifications,
                unverified,
            ),
        )
        connection.executemany(
            "INSERT INTO plan_projection_union_roots VALUES (?, ?, ?)",
            ((plan_id, index, root) for root in union.roots),
        )
        for row in unprocessable:
            relative_path = str(row["relative_path"])
            if any(
                is_within(relative_path, root) and relative_path != root
                for root in union.roots
            ):
                reason = skipped_reasons.get(
                    relative_path, f"{row['entry_kind']}: {row['read_outcome']}"
                )
                connection.execute(
                    "INSERT INTO plan_projection_omitted_entries VALUES (?, ?, ?, ?)",
                    (plan_id, index, relative_path, reason),
                )
    for index, group in enumerate(conflict_groups):
        connection.execute(
            "INSERT INTO plan_projection_conflict_groups VALUES (?, ?, ?, ?)",
            (plan_id, index, group.canonical_root, group.canonical_reason),
        )
        connection.executemany(
            "INSERT INTO plan_projection_conflict_group_roots VALUES (?, ?, ?)",
            ((plan_id, index, root) for root in group.roots),
        )
        for row in unprocessable:
            relative_path = str(row["relative_path"])
            if any(
                is_within(relative_path, root) and relative_path != root
                for root in group.roots
            ):
                reason = skipped_reasons.get(
                    relative_path, f"{row['entry_kind']}: {row['read_outcome']}"
                )
                connection.execute(
                    "INSERT INTO plan_projection_conflict_omitted_entries "
                    "VALUES (?, ?, ?, ?)",
                    (plan_id, index, relative_path, reason),
                )


def _retained_operations(
    connection: sqlite3.Connection, run_id: str
) -> list[PlanOperation]:
    """Every unique readable regular file plus one Canonical Copy per group.

    Non-canonical Exact Duplicate occurrences, Skipped Entry Findings, and
    content identities that were never proven successful never become an
    operation.
    """
    identity_rows = [
        row
        for row in select_identity_rows(connection, run_id)
        if row.read_outcome == READ_OUTCOME_SUCCESSFUL
    ]
    groups = derive_exact_duplicate_groups(identity_rows)

    operations: list[PlanOperation] = []
    identity_paths: set[str] = set()
    for group in groups:
        identity_paths.update(group.member_relative_paths)
        operations.append(
            PlanOperation(
                source_relative_path=group.canonical_relative_path,
                output_relative_path=group.canonical_relative_path,
                expected_byte_size=group.key.byte_size,
                algorithm=group.key.algorithm,
                algorithm_version=group.key.algorithm_version,
                digest=group.key.digest,
                canonical_reason=group.canonical_reason,
            )
        )

    for relative_path, byte_size, modified_ns in connection.execute(
        "SELECT relative_path, observed_byte_size, modified_ns FROM inventory_entries "
        "WHERE run_id = ? AND entry_kind = 'regular-file' AND read_outcome = ? "
        "ORDER BY relative_path",
        (run_id, READ_OUTCOME_SUCCESSFUL),
    ):
        path = str(relative_path)
        if path in identity_paths:
            continue
        operations.append(
            PlanOperation(
                source_relative_path=path,
                output_relative_path=path,
                expected_byte_size=int(byte_size),
                algorithm=None,
                algorithm_version=None,
                digest=None,
                canonical_reason="unique-size metadata observation",
                modified_ns=int(modified_ns),
                evidence_kind="metadata-observation",
            )
        )

    operations.sort(key=lambda operation: operation.source_relative_path)
    return operations


def _rooted_path(root: str, descendant: str) -> str:
    return descendant if root == "." else f"{root}/{descendant}"


def _rooted_unverified_counterpart(roots: tuple[str, ...], evidence: str) -> str:
    """Turn persisted left/right evidence into self-contained source paths."""
    for side, unprocessable_root, counterpart_root in (
        ("left", roots[0], roots[1]),
        ("right", roots[1], roots[0]),
    ):
        suffix = f" ({side} unprocessable)"
        if evidence.endswith(suffix):
            descendant = evidence.removesuffix(suffix)
            return (
                f"{_rooted_path(unprocessable_root, descendant)} unprocessable; "
                f"copyable counterpart: {_rooted_path(counterpart_root, descendant)}"
            )
    return evidence


def _unrelated_roots(left: str, right: str) -> bool:
    return not is_within(left, right) and not is_within(right, left)


def _structural_projections(
    connection: sqlite3.Connection, run_id: str
) -> list[StructuralUnion | LosslessConflictProjection]:
    """Return independently validated, non-nested structural projections.

    A relationship to an ancestor is inventory navigation, not a peer-wrapper
    union. Relationship connectivity proposes a group; complete corresponding
    path evidence independently decides whether it is a Structural Union or a
    Lossless Conflict Projection.
    """
    component_rows = connection.execute(
        "SELECT component_id, root_relative_path "
        "FROM actionable_exact_component_members "
        "WHERE run_id = ? ORDER BY component_id, root_relative_path",
        (run_id,),
    ).fetchall()
    components: dict[str, list[str]] = {}
    for row in component_rows:
        components.setdefault(str(row["component_id"]), []).append(
            str(row["root_relative_path"])
        )
    compatible_pairs: dict[tuple[str, str], sqlite3.Row] = {}
    adjacency: dict[str, set[str]] = defaultdict(set)
    exact_components = [tuple(roots) for roots in components.values() if len(roots) > 1]
    for component_roots in exact_components:
        for root in component_roots:
            adjacency.setdefault(root, set())
        for index, left in enumerate(component_roots):
            for right in component_roots[index + 1 :]:
                adjacency[left].add(right)
                adjacency[right].add(left)
    rows = connection.execute(
        "SELECT left_root, right_root, classification, canonical_root, canonical_reason FROM "
        "directory_relationships WHERE run_id = ? ORDER BY left_root, right_root",
        (run_id,),
    ).fetchall()
    rows.sort(
        key=lambda row: (
            str(row["left_root"]).count("/"),
            str(row["right_root"]).count("/"),
            str(row["left_root"]),
            str(row["right_root"]),
        )
    )
    for row in rows:
        left, right = str(row["left_root"]), str(row["right_root"])
        classification = str(row["classification"])
        if classification not in {
            "identical",
            "strict-subset",
            "strict-superset",
            "union-compatible",
        }:
            continue
        if not _unrelated_roots(left, right):
            continue
        pair = (min(left, right), max(left, right))
        compatible_pairs[pair] = row
        adjacency[left].add(right)
        adjacency[right].add(left)

    conflicting_rows = connection.execute(
        "SELECT left_root, right_root FROM directory_relationships "
        "WHERE run_id = ? AND classification = 'conflicting'",
        (run_id,),
    ).fetchall()
    for row in conflicting_rows:
        left, right = str(row["left_root"]), str(row["right_root"])
        if _unrelated_roots(left, right):
            adjacency[left].add(right)
            adjacency[right].add(left)
    projections: list[StructuralUnion | LosslessConflictProjection] = []
    visited: set[str] = set()
    for start in sorted(adjacency):
        if start in visited:
            continue
        stack: list[str] = [start]
        roots: set[str] = set()
        while stack:
            root = stack.pop()
            if root in visited:
                continue
            visited.add(root)
            roots.add(root)
            stack.extend(adjacency[root] - visited)
        ordered_roots = tuple(sorted(roots))
        if any(
            not _unrelated_roots(left, right)
            for index, left in enumerate(ordered_roots)
            for right in ordered_roots[index + 1 :]
        ):
            continue
        component_conflicts = component_has_conflict(connection, run_id, ordered_roots)
        if component_conflicts:
            projections.append(
                LosslessConflictProjection(
                    ordered_roots,
                    _canonical_component_root(list(ordered_roots)),
                    "shallowest relative path, then lexical relative-path order",
                )
            )
            continue
        exact_component = next(
            (
                component
                for component in exact_components
                if set(component) == set(ordered_roots)
            ),
            None,
        )
        if exact_component is not None:
            projections.append(
                StructuralUnion(
                    ordered_roots,
                    _canonical_component_root(list(ordered_roots)),
                    "exact-identity component",
                    "shallowest relative path, then lexical relative-path order",
                )
            )
        elif len(ordered_roots) == 2 and ordered_roots in compatible_pairs:
            row = compatible_pairs[ordered_roots]
            projections.append(
                StructuralUnion(
                    ordered_roots,
                    str(row["canonical_root"]),
                    str(row["classification"]),
                    str(row["canonical_reason"]),
                )
            )
        elif len(ordered_roots) > 1:
            projections.append(
                StructuralUnion(
                    ordered_roots,
                    _multi_tree_canonical_root(ordered_roots, compatible_pairs),
                    "multi-tree union",
                    "proven connected component of compatible structural relationships",
                )
            )
    ordered_projections = sorted(
        projections,
        key=lambda projection: (
            projection.canonical_root.count("/"),
            projection.canonical_root,
            projection.roots,
        ),
    )
    chosen: list[StructuralUnion | LosslessConflictProjection] = []
    occupied: set[str] = set()
    for projection in ordered_projections:
        if any(
            not _unrelated_roots(root, existing)
            for root in projection.roots
            for existing in occupied
        ):
            continue
        chosen.append(projection)
        occupied.update(projection.roots)
    return chosen


def _canonical_component_root(roots: list[str]) -> str:
    return min(roots, key=lambda path: (path.count("/"), path))


def _multi_tree_canonical_root(
    roots: tuple[str, ...], compatible_pairs: dict[tuple[str, str], sqlite3.Row]
) -> str:
    """Prefer a root proven to contain every peer; otherwise use stable order."""
    supersets = set(roots)
    for pair, row in compatible_pairs.items():
        if not set(pair) <= set(roots):
            continue
        classification = str(row["classification"])
        left, right = str(row["left_root"]), str(row["right_root"])
        if classification == "strict-subset":
            supersets.discard(left)
        elif classification == "strict-superset":
            supersets.discard(right)
    return _canonical_component_root(sorted(supersets or set(roots)))


def _structural_entries(
    connection: sqlite3.Connection,
    run_id: str,
    projections: list[StructuralUnion | LosslessConflictProjection],
) -> tuple[
    list[PlanOutputEntry],
    set[str],
    set[str],
    list[tuple[int, ConflictSourceMapping]],
]:
    """Project validated Structural Unions and Lossless Conflict Projections."""
    entries: list[PlanOutputEntry] = []
    covered_files: set[str] = set()
    planned_roots: set[str] = set()
    conflict_mappings: list[tuple[int, ConflictSourceMapping]] = []
    identity_rows = {
        str(row["relative_path"]): row
        for row in connection.execute(
            "SELECT relative_path, algorithm, algorithm_version, byte_size, digest "
            "FROM content_identities WHERE run_id = ? AND read_outcome = 'successful'",
            (run_id,),
        )
    }
    inventory = connection.execute(
        "SELECT relative_path, entry_kind, read_outcome FROM inventory_entries "
        "WHERE run_id = ? ORDER BY relative_path",
        (run_id,),
    ).fetchall()
    inventory_paths = {str(row["relative_path"]) for row in inventory}
    conflict_namespace = "__fso-conflicts__"
    suffix = 0
    while any(
        path == conflict_namespace or path.startswith(f"{conflict_namespace}/")
        for path in inventory_paths
    ):
        suffix += 1
        conflict_namespace = f"__fso-conflicts__-{suffix}"
    conflict_group_index = 0
    for projection in projections:
        roots = projection.roots
        canonical = projection.canonical_root
        planned_roots.update(roots)
        if isinstance(projection, LosslessConflictProjection):
            result = project_lossless_conflicts(
                connection,
                run_id,
                roots,
                canonical,
                conflict_namespace,
            )
            entries.extend(result.entries)
            covered_files.update(result.covered_files)
            conflict_mappings.extend(
                (conflict_group_index, mapping) for mapping in result.mappings
            )
            conflict_group_index += 1
            continue

        projected: dict[str, list[str]] = {}
        directories: set[str] = {canonical}
        for root in roots:
            for row in inventory:
                source = str(row["relative_path"])
                if not is_within(source, root) or source == root:
                    continue
                relative = source[len(root) + 1 :] if root != "." else source
                output = f"{canonical}/{relative}" if canonical != "." else relative
                if (
                    row["entry_kind"] == "directory"
                    and row["read_outcome"] == READ_OUTCOME_SUCCESSFUL
                ):
                    directories.add(output)
                elif row["entry_kind"] == "regular-file" and source in identity_rows:
                    projected.setdefault(output, []).append(source)
                    covered_files.add(source)
        reason = (
            f"Structural Union ({projection.classification}); "
            f"canonical directory root: {canonical}"
        )
        entries.extend(
            PlanOutputEntry("directory", path, None, None, None, None, None, reason)
            for path in sorted(directories)
            if path != "."
        )
        for output, sources in sorted(projected.items()):
            source = min(sources)
            identity = identity_rows[source]
            entries.append(
                PlanOutputEntry(
                    "file",
                    output,
                    source,
                    int(identity["byte_size"]),
                    str(identity["algorithm"]),
                    int(identity["algorithm_version"]),
                    str(identity["digest"]),
                    reason,
                )
            )
    return entries, covered_files, planned_roots, conflict_mappings


def _validate_output_entries(entries: list[PlanOutputEntry]) -> None:
    by_path = {entry.output_relative_path: entry for entry in entries}
    if len(by_path) != len(entries):
        raise ConsolidationPlanError(
            "Structural Union projections contain duplicate output paths"
        )
    ordered_paths = sorted(by_path)
    for path, entry in by_path.items():
        for parent in Path(path).parents:
            parent_path = parent.as_posix()
            if parent_path == ".":
                break
            parent_entry = by_path.get(parent_path)
            if parent_entry is not None and parent_entry.entry_kind != "directory":
                raise ConsolidationPlanError(
                    f"Output path conflict: {parent_path} is a file parent of {path}"
                )
        descendant_index = bisect_left(ordered_paths, f"{path}/")
        if (
            entry.entry_kind == "file"
            and descendant_index < len(ordered_paths)
            and ordered_paths[descendant_index].startswith(f"{path}/")
        ):
            raise ConsolidationPlanError(
                f"Output path conflict: file conflicts with directory: {path}"
            )
